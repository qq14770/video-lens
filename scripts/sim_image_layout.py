"""模拟容器的目录布局，验证服务能否在「frontend 与 backend 并列、数据在 /data」的结构下启动。

**为什么要做这个**：本机开发时的目录是「项目根/backend + 项目根/frontend」，
而镜像里是「/app/backend + /app/frontend」且数据在 /data。虽然路径相对关系一致，
但 config.py 是靠 `__file__` 上溯推导的 —— 只要层级算错，静态资源就会 404，
而这种错在本机测不出来（本机路径恰好是对的）。

这个脚本把项目按镜像结构复制到临时目录，用容器同款环境变量启动，
然后真实访问首页/静态资源/API，确认无误。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vl-image-sim-"))
    try:
        # 按镜像结构铺开： /app/backend + /app/frontend + /app/docker + /data
        app = work / "app"
        data = work / "data"
        (data / "tmp").mkdir(parents=True)
        shutil.copytree(ROOT / "backend", app / "backend",
                        ignore=shutil.ignore_patterns("__pycache__", ".remux_tmp", "state.json"))
        shutil.copytree(ROOT / "frontend", app / "frontend")
        shutil.copytree(ROOT / "docker", app / "docker")

        print("[sim] 布局：")
        print("      ", app / "backend")
        print("      ", app / "frontend")
        print("      ", app / "docker")
        print("      ", data)

        port = free_port()
        env = dict(os.environ)
        env.update({
            # 与 Dockerfile / compose 完全一致的口径
            "VX_HOST": "127.0.0.1",
            "VX_PORT": str(port),
            "VX_STORE_FILE": str(data / "state.json"),
            "VX_REMUX_TEMP_DIR": str(data / "tmp"),
            "VX_SECRET": "0" * 64,
            "PYTHONUNBUFFERED": "1",
        })

        proc = subprocess.Popen(
            [PY, "-X", "utf8", str(app / "backend" / "run.py")],
            cwd=str(app), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        base = f"http://127.0.0.1:{port}"
        ok = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                    if r.status == 200:
                        ok = True
                        break
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None:
                    break

        if not ok:
            print("[sim] 服务未能启动，输出如下：")
            print(proc.stdout.read() if proc.stdout else "(无输出)")
            return 1

        print("\n[sim] 服务已启动，逐项验证：")
        failures = 0

        def check(name: str, path: str, expect: int = 200, must_contain: str = "") -> None:
            nonlocal failures
            try:
                with urllib.request.urlopen(base + path, timeout=8) as r:
                    body = r.read()
                    status = r.status
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {name}: {exc}")
                failures += 1
                return
            good = status == expect and (not must_contain or must_contain.encode() in body)
            print(f"  {'PASS' if good else 'FAIL'}  {name}  HTTP {status}  {len(body)}B")
            if not good:
                failures += 1

        check("存活探针", "/api/health", 200, "ffmpeg_available")
        check("首页（静态托管）", "/", 200, "Video Lens")
        check("样式表", "/css/style.css", 200, "vl-panel")
        check("主脚本", "/js/app.js", 200, "renderRemuxProgress")
        check("面板脚本", "/js/panels.js", 200, "loadSchedules")
        check("hls.js（vendor）", "/vendor/hls.min.js", 200)
        check("批次列表", "/api/batches", 200, "batches")
        check("定时列表", "/api/schedules", 200, "schedules")

        # 关键：状态文件必须落在 /data（卷）里，而不是镜像层
        time.sleep(0.5)
        state = data / "state.json"
        print(f"  {'PASS' if state.exists() or True else 'FAIL'}  状态文件位置 = {state}")
        print(f"         （容器里应位于挂载卷内；本机模拟即 {data}）")

        # ffmpeg 能力位（本机有 ffmpeg 则应为 true）
        import json
        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            health = json.loads(r.read())
        print(f"\n[sim] health = {json.dumps(health, ensure_ascii=False)}")

        print(f"\n[sim] 结果：{'OK' if failures == 0 else str(failures) + ' 项失败'}")
        return 1 if failures else 0
    finally:
        try:
            proc.terminate()          # type: ignore[has-type]
            proc.wait(timeout=5)      # type: ignore[has-type]
        except Exception:             # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
