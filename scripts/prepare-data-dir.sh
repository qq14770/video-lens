#!/usr/bin/env bash
# =============================================================================
#  prepare-data-dir.sh —— 建好数据目录并设对属主（容器部署前跑一次）
#  ---------------------------------------------------------------------------
#  为什么必须单独做这一步：
#    compose 里把宿主目录挂到容器的 /data。如果那个目录不存在，Docker 会**自动创建**，
#    而自动创建出来的属主是 root:root、权限 755 —— 容器内以 uid 1000 运行，
#    于是写不进去，表现为「定时任务存不了」「合并产物写不出」。
#
#    手动先建好并 chown 1000:1000，就能避开这个坑。
#
#  用法：
#      sudo bash scripts/prepare-data-dir.sh [数据目录]
#  不传参数时，会尝试从 docker-compose.yml 里读出实际映射的宿主路径。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

COMPOSE="${COMPOSE_FILE:-docker-compose.yml}"

# ---- 确定数据目录：优先命令行参数，其次从 compose 里解析 ----
DATA_DIR="${1:-}"

if [[ -z "$DATA_DIR" ]]; then
    if [[ ! -f "$COMPOSE" ]]; then
        echo "✗ 找不到 $COMPOSE，请显式传入数据目录："
        echo "    sudo bash scripts/prepare-data-dir.sh /vol1/1000/video-lens-data"
        exit 1
    fi
    # 从 compose 里解析出宿主路径，例如：
    #     - /vol1/1000/video-lens-data:/data
    #     - "/vol2/data/video-lens:/data"
    # 用 awk 而不是 grep+sed 串联：只需要「取最后一行冒号左边的部分」这一件事，
    # 嵌套引号在 sed 里极易写错（第一版就踩了），awk 直接按字段切更清楚。
    DATA_DIR="$(awk '
        /^[[:space:]]*-[[:space:]]+.*:\/data([[:space:]]|$)/ {
            line = $0
            sub(/^[[:space:]]*-[[:space:]]+/, "", line)   # 去掉列表符号
            gsub(/"/, "", line)                            # 去掉可能存在的引号
            sub(/:\/data.*$/, "", line)                    # 去掉 :/data 及之后
            last = line
        }
        END { if (last != "") print last }
    ' "$COMPOSE")"
fi

if [[ -z "$DATA_DIR" ]]; then
    echo "✗ 无法从 $COMPOSE 解析出数据目录"
    echo "  请显式指定： sudo bash scripts/prepare-data-dir.sh /vol1/1000/video-lens-data"
    exit 1
fi

# ---- 安全检查：绝不允许在危险的顶层目录上操作 ----
case "$DATA_DIR" in
    /|/vol1|/vol2|/vol3|/vol4|/volume1|/mnt|/home|/root|/etc|/usr|/var)
        echo "✗ 拒绝对 $DATA_DIR 操作 —— 这看起来是系统级目录，不像数据目录。"
        exit 1
        ;;
esac
if [[ "$DATA_DIR" != /* ]]; then
    echo "✗ 请提供绝对路径（当前：$DATA_DIR）"
    exit 1
fi

echo "=============================================================="
echo " 准备数据目录：$DATA_DIR"
echo "=============================================================="

# ---- 创建（幂等）----
if [[ -d "$DATA_DIR" ]]; then
    echo "✓ 目录已存在"
else
    mkdir -p "$DATA_DIR"
    echo "✓ 已创建目录"
fi

# 顺手建出 tmp 子目录（对应 VX_REMUX_TEMP_DIR=/data/tmp）
mkdir -p "$DATA_DIR/tmp"
echo "✓ 已确保 $DATA_DIR/tmp 存在"

# ---- 设属主与权限 ----
if [[ "$(id -u)" -ne 0 ]]; then
    echo "⚠ 当前不是 root，无法 chown。请用 sudo 重跑本脚本。"
    exit 1
fi
chown -R 1000:1000 "$DATA_DIR"
chmod 755 "$DATA_DIR"
echo "✓ 属主已设为 1000:1000，权限 755"

# ---- 写权限实测（以 1000 的身份）----
echo
echo "---- 实测 uid 1000 能否写入 ----"
if command -v setpriv >/dev/null 2>&1; then
    if setpriv --reuid=1000 --regid=1000 --clear-groups \
              sh -c "touch '$DATA_DIR/.write-test' && rm -f '$DATA_DIR/.write-test'" 2>/dev/null; then
        echo "✓ uid 1000 可以写入（容器内能正常保存定时计划与产物）"
    else
        echo "✗ uid 1000 仍然写不进去 —— 请把这条输出贴出来"
        ls -ld "$DATA_DIR"
        exit 1
    fi
elif command -v su >/dev/null 2>&1; then
    if su -s /bin/sh -c "touch '$DATA_DIR/.write-test' && rm -f '$DATA_DIR/.write-test'" "$(id -un 1000 2>/dev/null || echo 1000)" 2>/dev/null; then
        echo "✓ uid 1000 可以写入"
    else
        echo "⚠ 无法用 su 模拟 uid 1000（不影响使用，可忽略）"
    fi
else
    echo "⚠ 没有 setpriv/su，跳过实测（不影响使用）"
fi

echo
echo "---- 当前状态 ----"
ls -ld "$DATA_DIR" "$DATA_DIR/tmp"

echo
echo "=============================================================="
echo " 完成。接下来："
echo "     sudo docker compose up -d --build"
echo "     sudo docker compose logs -f"
echo "=============================================================="
