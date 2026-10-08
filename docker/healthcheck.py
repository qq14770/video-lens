"""容器健康检查：探测本机服务是否真的可用。

**为什么不直接在 Dockerfile 里写 ``python -c "..."``**：
那段命令要跨行书写，得同时处理 Dockerfile 的续行符 ``\\``、shell 的双引号、
以及 Python 自身的语句分隔 —— 三层转义叠在一起，任何一层出错都会变成
"容器永远 unhealthy"这种很难查的故障（而且报错信息只显示命令本身）。
写成独立文件后，Dockerfile 里只剩一条无歧义的 ``CMD ["python", "..."]

**为什么打 /api/health 而不是探端口**：这个接口会顺带报告 ffmpeg 能力位，
所以"健康"的含义是"能正常干活"，而不只是"进程还在监听"。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

#: 容器内固定 8300；允许用环境变量覆盖以便本地调试复用本脚本
PORT = os.getenv("VX_PORT", "8300")
URL = f"http://127.0.0.1:{PORT}/api/health"
TIMEOUT = 4


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                print(f"healthcheck: HTTP {resp.status}", file=sys.stderr)
                return 1
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"healthcheck: {URL} 不可达 —— {exc}", file=sys.stderr)
        return 1

    if not body.get("ok"):
        print(f"healthcheck: 服务自报不健康 {body}", file=sys.stderr)
        return 1

    # ffmpeg 缺失只提醒不判死：其余功能仍可用（下载会退化成存清单）
    if not body.get("ffmpeg_available"):
        print("healthcheck: 注意 ffmpeg 不可用，HLS 合并下载将降级",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
