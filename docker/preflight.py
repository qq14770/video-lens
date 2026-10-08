"""容器启动前的自检：把"起不来"的原因直接打在日志第一屏。

**为什么需要它**：容器在 ``Restarting`` 循环里时，用户能看到的往往只有
``Restarting (1) 2 seconds ago`` 这一行状态 —— 没有栈、没有上下文，无从下手。
这里在导入业务代码之前先把**运行环境**逐项确认并打印，于是：

  * 一切正常 → 日志第一屏就是清晰的版本/路径/能力清单，便于确认部署成功；
  * 有问题 → 明确写出"哪一项、为什么、怎么办"，而不是等某个 import 抛栈。

**检查顺序刻意如此**：从"最可能让容器直接起不来"排到"只影响功能"。
前四项失败会退出（它们必然导致服务不可用），后面的只告警。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import sysconfig
from pathlib import Path

#: 必须能导入的核心依赖（缺任何一个服务都跑不起来）
REQUIRED = ("fastapi", "uvicorn", "pydantic", "httpx", "yt_dlp")


def _line(ok: bool, label: str, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[preflight] {mark} {label}" + (f"  {detail}" if detail else ""),
          flush=True)


def main() -> int:
    print("[preflight] ---- 容器启动自检 ----", flush=True)
    print(f"[preflight] python {sys.version.split()[0]} @ {sys.executable}",
          flush=True)
    print(f"[preflight] cwd={os.getcwd()}", flush=True)

    problems: list[str] = []

    # ---- ① 依赖能否导入（.pth 链接是否生效）----
    for mod in REQUIRED:
        try:
            m = importlib.import_module(mod)
            _line(True, f"import {mod}", getattr(m, "__version__", ""))
        except Exception as exc:  # noqa: BLE001
            _line(False, f"import {mod}", f"{type(exc).__name__}: {exc}")
            problems.append(
                f"依赖 {mod} 导入失败 —— 多半是构建期 `pip install --prefix=/install` "
                f"的产物没接进 sys.path。\n"
                f"      排查： python -c \"import sysconfig;print(sysconfig.get_paths()['purelib'])\"\n"
                f"             ls /install/lib/python*/site-packages | head\n"
                f"      修复： 重新构建（docker compose build --no-cache）"
            )

    if problems:
        print("\n[preflight] 依赖检查未通过，服务无法启动：", flush=True)
        for p in problems:
            print(f"  → {p}", flush=True)
        return 1

    # ---- ② ffmpeg（只影响 HLS 合并，不阻止启动）----
    exe = shutil.which(os.getenv("VX_FFMPEG_PATH", "ffmpeg") or "ffmpeg")
    _line(bool(exe), "ffmpeg", exe or "未找到 —— HLS 合并下载将降级为存清单")
    if not exe:
        print("[preflight] 提示：镜像应内置 ffmpeg。若缺失，检查 apt 安装步骤。",
              flush=True)

    # ---- ③ 静态资源（缺失则页面 404，但服务能起）----
    # config.STATIC_DIR 靠 __file__ 上溯推导，这里提前确认它算对了
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
        from app.config import config  # noqa: PLC0415
        static = Path(config.STATIC_DIR)
        has_index = (static / "index.html").is_file()
        _line(has_index, "前端静态目录", str(static))
        if not has_index:
            print(f"[preflight] 提示：{static} 下没有 index.html —— "
                  f"检查 Dockerfile 是否漏拷 frontend/", flush=True)
    except Exception as exc:  # noqa: BLE001
        _line(False, "读取配置", f"{type(exc).__name__}: {exc}")
        print("[preflight] 这属于代码/构建问题，请把以上输出贴出来。", flush=True)
        return 1

    # ---- ④ 可写目录（不阻止启动，但会让定时/下载静默失效）----
    for label, path in (("状态目录", os.path.dirname(os.path.abspath(config.STORE_FILE))),
                        ("临时产物目录", config.REMUX_TEMP_DIR)):
        ok, detail = _check_writable(path)
        _line(ok, f"{label}可写", detail)
        if not ok:
            print(f"[preflight]      → 运行身份：{_whoami()}", flush=True)
            print("[preflight]        容器以 uid 1000 运行时，宿主目录属主必须一致；"
                  "在 NAS 上执行：", flush=True)
            print(f"[preflight]          sudo chown -R 1000:1000 {path}", flush=True)
            print("[preflight]        或者改用 root 运行（compose 里注释掉 user: 那一行），"
                  "就无需 chown。", flush=True)

    print("[preflight] ---- 自检结束，交给应用 ----", flush=True)
    return 0


def _whoami() -> str:
    """当前运行身份，附在告警里 —— 判断权限问题时这是第一条要看的线索。"""
    uid, gid = os.getuid(), os.getgid()      # 仅 Linux 容器会走到这里
    return f"uid={uid} gid={gid}" + ("（root）" if uid == 0 else "")


def _check_writable(path: str) -> tuple[bool, str]:
    """探测目录可写；返回 ``(是否可写, 展示用说明)``。

    先尝试创建目录（宿主目录可能还不存在），再写一个探针文件并删除。
    失败时**同时报告当前属主/权限**，因为"谁是属主"正是这类故障的关键信息。
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return False, f"{path} —— 无法创建目录：{exc}"

    probe = os.path.join(path, ".preflight-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True, path
    except OSError as exc:
        # 附上属主信息，让用户一眼看出该 chown 给谁
        try:
            st = os.stat(path)
            owner = f"属主 uid={st.st_uid} gid={st.st_gid} 权限={oct(st.st_mode & 0o777)}"
        except OSError:
            owner = "无法读取属主信息"
        return False, f"{path} —— {exc}（{owner}）"


if __name__ == "__main__":
    raise SystemExit(main())
