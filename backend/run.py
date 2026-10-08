"""一键启动入口：``python backend/run.py``

  - 默认读取环境变量 VX_HOST / VX_PORT（缺省 127.0.0.1:8300）；
  - ``--reload`` 仅供开发热重载，生产请勿开启；
  - 启动前自检依赖是否装齐，缺包时给出明确的安装指令而不是裸 ImportError。
"""

from __future__ import annotations

import argparse
import sys


def _precheck() -> None:
    missing = []
    for pkg in ("fastapi", "uvicorn", "yt_dlp", "httpx", "pydantic"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print("[Video Lens] 缺少依赖：", ", ".join(missing))
        print("请先执行：python -m pip install -r backend/requirements.txt")
        sys.exit(1)


def main() -> None:
    _precheck()
    parser = argparse.ArgumentParser(description="Video Lens 视频内容提取服务")
    parser.add_argument("--host", default=None, help="监听地址（默认取 VX_HOST）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取 VX_PORT）")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码改动自动重启")
    args = parser.parse_args()

    # 让 backend/ 目录成为可导入路径（无需安装成包）
    import os

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app.config import config

    import uvicorn

    host = args.host or config.HOST
    port = args.port or config.PORT
    print(f"[Video Lens] http://{host}:{port}  （API 文档：/api/docs）")
    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
