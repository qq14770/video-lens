"""容器启动失败排查助手。

不依赖 Docker：把镜像启动链路上的**每一个环节**逐个检查，
并指出最可能出问题的地方。用法：

    python -X utf8 scripts/diag_container.py

它以只读方式检查本地项目，并打印「容器里对应的检查命令」——
这样你在飞牛上能直接照抄执行。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def hr(title: str) -> None:
    print()
    print("=" * 72)
    print(" " + title)
    print("=" * 72)


def main() -> int:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    hr("① 镜像启动链路（Dockerfile 里的实际命令）")
    for pat in (r"^ENTRYPOINT\s+(\[.*\])", r"^CMD\s+(\[.*\])", r"^\s*CMD\s+(\[.*\])"):
        m = re.search(pat, dockerfile, re.M)
        if m:
            print(f"  {pat.split(chr(92))[0][:12]:14s} {m.group(1)}")
    print()
    print("  展开后容器实际执行：")
    print("    /usr/bin/tini -- python backend/run.py")
    print("  ↑ 这条链路上任何一环不存在，容器都会立刻退出并进入 Restarting 循环。")

    hr("② 三个最可能的失败点（按概率排序）")
    print("""
  [A] tini 不在 /usr/bin/tini
      apt 装的 tini 在 debian 上确实是 /usr/bin/tini，但如果基础镜像或
      apt 源有差异，路径就可能是 /usr/bin/tini-static 或不存在。
      → 容器会以 exit 127 立刻退出。
      验证（在飞牛上）：docker run --rm --entrypoint sh <镜像> -c "ls -l /usr/bin/tini"

  [B] python 找不到依赖（/install 没接进 sys.path）
      .pth 文件若没生效，import fastapi 就失败，run.py 会打印
      「缺少依赖」并 sys.exit(1) —— 这会在日志里留下**非常明确**的一行。
      验证： docker run --rm --entrypoint python <镜像> -c "import fastapi;print('ok')"

  [C] /data 不可写
      这种情况**不会**导致重启（启动自检只告警不退出），
      所以如果你的现象是反复重启，可以排除它。
""")

    hr("③ 你现在应该执行的两条命令（最关键）")
    print("""
  # 1) 看日志 —— 重启循环里日志是唯一的信息来源
  sudo docker logs video-lens --tail 100

  # 2) 看退出码 —— 区分「代码报错」与「被系统杀掉」
  sudo docker inspect video-lens --format '{{.State.ExitCode}} {{.State.Error}}'
""")
    print("  退出码速查：")
    print("    0    → 进程正常退出（说明 run.py 走完了，通常是 uvicorn 绑定失败）")
    print("    1    → run.py 的 sys.exit(1)，多为「缺少依赖」")
    print("    127  → 找不到可执行文件，几乎一定是 tini 或 python 路径问题")
    print("    137  → 被 SIGKILL（多为 OOM，NAS 内存不足）")
    print("    143  → 被 SIGTERM")

    hr("④ 本地静态复核（不需要 Docker）")

    # 检查 compose 里是否还留着占位密钥（这不是重启原因，但顺带提醒）
    m = re.search(r'VX_SECRET:\s*"([^"]*)"', compose)
    if m and any(k in m.group(1) for k in ("请替换", "CHANGE", "changeme")):
        print("  ⚠ docker-compose.yml 的 VX_SECRET 仍是占位值（不会导致重启，但需更换）")
    else:
        print("  VX_SECRET 已改（或未设置）")

    # 目录挂载路径
    vols = re.findall(r'-\s+([^\s:]+):(/data)', compose)
    for host, cont in vols:
        print(f"  卷映射：{host} → {cont}")
        print(f"         （飞牛上请确认该宿主目录存在且属主为 1000:1000）")

    # 端口
    ports = re.findall(r'-\s+"(\d+):(\d+)"', compose)
    for host, cont in ports:
        print(f"  端口映射：宿主 {host} → 容器 {cont}")

    # 语法复核
    hr("⑤ 语法复核")
    ok = True
    for m in re.finditer(r'^(ENTRYPOINT|CMD|VOLUME)\s+(\[.*\])\s*$', dockerfile, re.M):
        try:
            json.loads(m.group(2))
        except json.JSONDecodeError as exc:
            print(f"  ✗ {m.group(1)} 数组非法：{exc}")
            ok = False
    print("  exec 数组格式:", "OK" if ok else "有问题")

    # run.py 的 precheck 会 sys.exit(1)
    run_py = (ROOT / "backend" / "run.py").read_text(encoding="utf-8")
    if "sys.exit(1)" in run_py:
        print("  run.py 在缺依赖时会 sys.exit(1)（退出码 1 时优先查这条）")

    hr("⑥ 如果确认是 tini 问题：两个可选修法")
    print("""
  修法 1（推荐）：不用 tini，改用 python 自带的信号处理
      Dockerfile 改成：
        ENTRYPOINT ["python", "backend/run.py"]
      并在 run.py 里让 uvicorn 正常响应 SIGTERM（uvicorn 默认就会）。

  修法 2：保留 tini 但先确认路径
      把 ENTRYPOINT 改成 sh -c 形式，启动时打印实际路径再 exec：
        ENTRYPOINT ["sh", "-c", "command -v tini || ls -l /usr/bin/tini*; exec \\"$@\\"", "--"]
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
