#!/usr/bin/env bash
# =============================================================================
#  fix-perms.sh —— 一键修好「Windows 拷过来的文件权限全空」问题
#  ---------------------------------------------------------------------------
#  什么时候需要它：
#    从 Windows 用 SMB / zip / 文件管理上传项目到飞牛后，文件权限位常常变成 000
#    （`ls -la` 显示 `----------`，目录显示 `d---------`）。于是：
#      * `docker compose build` 报 "no configuration file provided: not found"
#        —— 它读不到 docker-compose.yml；
#      * 容器起来后报 `[Errno 13] Permission denied` 读不了 run.py。
#
#  用法（在项目根目录，即含 docker-compose.yml 的那一层执行）：
#      sudo bash scripts/fix-perms.sh
#
#  它做三件事：
#      1. 目录 → 755（需要可进入/遍历），文件 → 644（可读）；
#      2. 属主 → 1000:1000（容器内进程的身份，飞牛默认管理员也是 1000）；
#      3. 打印修复前后的对比，让你能确认真的生效了。
#
#  为什么属主要设成 1000：
#    容器以非 root 的 uid 1000 运行。若宿主文件属主是 root 或其他 uid，
#    容器内就写不了（/data 挂载目录尤其明显）。
# =============================================================================
set -euo pipefail

# ---- 定位项目根（脚本所在目录的上一级）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

echo "=============================================================="
echo " 修复目录权限：$ROOT"
echo "=============================================================="

# ---- 必须先确认这是项目根，避免手滑在别的目录递归 chmod ----
if [[ ! -f docker-compose.yml && ! -f Dockerfile ]]; then
    echo "✗ 当前目录既没有 docker-compose.yml 也没有 Dockerfile"
    echo "  请把本脚本放在项目的 scripts/ 下执行，或先 cd 到项目根目录。"
    echo "  当前目录：$ROOT"
    exit 1
fi
echo "✓ 确认为项目根目录（找到 docker-compose.yml / Dockerfile）"

# ---- 修复前快照 ----
echo
echo "---- 修复前 ----"
ls -la | head -15

# ---- 执行修复 ----
echo
echo "---- 正在修复 ----"
# 目录需要 +x 才能进入；文件只需 +r（.py 由 python 解释执行，不需要可执行位）
find . -type d -exec chmod 755 {} +
find . -type f -exec chmod 644 {} +
echo "✓ 权限位已规范化（目录 755 / 文件 644）"

# 属主：只有在能 chown 时才做（非 root 跑会失败，给出提示而不是硬崩）
if [[ "$(id -u)" -eq 0 ]]; then
    chown -R 1000:1000 . 2>/dev/null || {
        echo "⚠ chown 失败（该文件系统可能不支持，如某些 SMB 挂载）"
        echo "  这不影响构建与运行，只是 /data 写入可能受限。"
    }
    echo "✓ 属主已设为 1000:1000"
else
    echo "⚠ 当前不是 root，跳过 chown。若容器报目录不可写，请用 sudo 重跑本脚本。"
fi

# ---- 修复后验证 ----
echo
echo "---- 修复后 ----"
ls -la | head -15

# ---- 关键文件可读性自检 ----
echo
echo "---- 关键文件可读性 ----"
failed=0
for f in docker-compose.yml Dockerfile backend/run.py backend/requirements.txt \
         frontend/index.html docker/preflight.py; do
    if [[ -r "$f" ]]; then
        echo "  OK   $f"
    else
        echo "  FAIL $f （不可读！）"
        failed=1
    fi
done

echo
if [[ "$failed" -eq 0 ]]; then
    echo "=============================================================="
    echo " 完成。现在可以构建了："
    echo "     sudo docker compose build --no-cache"
    echo "     sudo docker compose up -d"
    echo "=============================================================="
else
    echo "仍有文件不可读 —— 请把上面的输出贴出来。"
    exit 1
fi
