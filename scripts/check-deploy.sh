#!/usr/bin/env bash
# =============================================================================
#  check-deploy.sh —— 构建前的一次性自检（在飞牛上跑）
#  ---------------------------------------------------------------------------
#  解决的问题：反复遇到「改了代码但容器行为没变」。
#  原因通常是三种之一：
#    1. 构建失败，但 compose 复用了旧镜像（up -d 不会重建）；
#    2. Dockerfile 需要的文件在 NAS 上缺失（例如后加的 docker/ 目录没同步）；
#    3. 源码权限位不对，构建时读不到（COPY 会直接失败）。
#
#  本脚本把这三件事一次查完，并给出下一步该执行的命令。
#
#  用法：
#      sudo bash scripts/check-deploy.sh
# =============================================================================
set -uo pipefail   # 故意不用 -e：要跑完全部检查再汇总

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

PROBLEMS=0
note() { echo "  $*"; }
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; PROBLEMS=$((PROBLEMS + 1)); }

echo "=============================================================="
echo " 部署自检：$ROOT"
echo "=============================================================="

# ---------------------------------------------------------------- 1. 必需文件
echo
echo "[1/5] 必需文件是否齐全（缺任何一个都会让构建失败）"
required=(
    Dockerfile
    docker-compose.yml
    backend/run.py
    backend/requirements.txt
    frontend/index.html
    docker/link_install.py
    docker/preflight.py
    docker/healthcheck.py
)
for f in "${required[@]}"; do
    if [[ -r "$f" ]]; then
        ok "$f"
    else
        if [[ -e "$f" ]]; then
            bad "$f 存在但**不可读**（权限位问题 → 跑 scripts/fix-perms.sh）"
        else
            bad "$f **缺失**（该项目文件没同步到 NAS）"
        fi
    fi
done

# ---------------------------------------------------------------- 2. 旧版残留检测
echo
echo "[2/5] Dockerfile 是否为新版（含 chmod 修复与 preflight）"
if grep -q "preflight.py" Dockerfile 2>/dev/null; then
    ok "ENTRYPOINT 含 preflight（新版）"
else
    bad "Dockerfile 里没有 preflight —— 这是旧版，请同步最新 Dockerfile"
fi
if grep -q "type d -exec chmod 755" Dockerfile 2>/dev/null; then
    ok "含显式 chmod 修复（解决 zip 丢失权限位）"
else
    bad "缺显式 chmod —— 旧版 Dockerfile，请同步"
fi
if grep -qE "^USER " Dockerfile 2>/dev/null; then
    note "注意：Dockerfile 里有生效的 USER 指令 → 以非 root 运行"
    note "      此时宿主数据目录属主必须是 1000:1000（跑 prepare-data-dir.sh）"
else
    ok "无生效的 USER 指令 → 以 root 运行（无需 chown 数据目录）"
fi

# ---------------------------------------------------------------- 3. 镜像新旧
echo
echo "[3/5] 现有镜像的新旧（旧镜像会让你的改动完全看不到）"
if command -v docker >/dev/null 2>&1; then
    img="$(docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' 2>/dev/null \
           | grep -i 'video' | head -3)"
    if [[ -n "$img" ]]; then
        echo "$img" | while read -r line_; do note "$line_"; done
        note "↑ 对比 Dockerfile 的修改时间：若镜像更早，说明改动没进镜像"
        ls -l --time-style=+%Y-%m-%d\ %H:%M Dockerfile | awk '{print "      Dockerfile 修改于 " $6 " " $7}'
    else
        note "还没构建过镜像"
    fi
else
    note "（本机没有 docker 命令，跳过）"
fi

# ---------------------------------------------------------------- 4. 数据目录
echo
echo "[4/5] 数据目录（compose 里映射到容器的 /data）"
DATA_DIR="$(awk '
    /^[[:space:]]*-[[:space:]]+.*:\/data([[:space:]]|$)/ {
        line = $0
        sub(/^[[:space:]]*-[[:space:]]+/, "", line)
        gsub(/"/, "", line)
        sub(/:\/data.*$/, "", line)
        last = line
    }
    END { if (last != "") print last }
' docker-compose.yml 2>/dev/null)"

if [[ -n "$DATA_DIR" ]]; then
    note "映射路径：$DATA_DIR"
    if [[ -d "$DATA_DIR" ]]; then
        ok "目录已存在"
        ls -ld "$DATA_DIR" | awk '{print "      属主/权限：" $3 ":" $4 " " $1}'
    else
        note "目录还不存在（Docker 会自动创建为 root:root，root 模式下没问题）"
    fi
else
    bad "无法从 docker-compose.yml 解析出数据目录 —— 检查 volumes 段"
fi

# ---------------------------------------------------------------- 5. 下一步
echo
echo "[5/5] 结论"
echo "=============================================================="
if [[ "$PROBLEMS" -eq 0 ]]; then
    echo " 自检通过。执行下面这条**完整**命令（不要只跑 up -d）："
    echo
    echo "     sudo docker compose build --no-cache"
    echo "     sudo docker compose up -d --force-recreate"
    echo "     sudo docker compose logs -f"
    echo
    echo " 为什么必须带上 --no-cache 与 --force-recreate："
    echo "   * build --no-cache  绕过构建缓存，确保 Dockerfile 的改动真的生效"
    echo "   * up --force-recreate  即使镜像变了也重建容器（否则可能继续用旧容器）"
    echo
    echo " 预期日志第一行是：  [preflight] ---- 容器启动自检 ----"
    echo " 若没有这一行，说明跑的还是旧镜像。"
else
    echo " 发现 $PROBLEMS 个问题（见上面的 ✗）。先修掉它们，"
    echo " 再执行 build --no-cache && up -d --force-recreate。"
fi
echo "=============================================================="
exit "$PROBLEMS"
