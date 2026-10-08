"""把 ``--prefix`` 安装的依赖接进 Python 的模块搜索路径。

**为什么需要它**：镜像用多阶段构建 —— 依赖装在 builder 层的 ``/install`` 前缀里
（这样编译期工具留在 builder，运行层保持精简）。但 Python 默认不会去
``/install/lib/pythonX.Y/site-packages`` 找包，必须显式告知。

**为什么不直接写 ``ENV PYTHONPATH=/install/lib/python3.12/site-packages``**：
把版本号写死，基础镜像一旦从 3.12 升到 3.13，路径就指向不存在的目录 ——
而且是**静默失效**：镜像能构建成功，容器一起来才 ImportError。
这里按运行时实际版本号算出路径并写成 ``.pth`` 文件，换基础镜像也不会坏。

**为什么用 .pth 而不是环境变量**：``.pth`` 由 site 模块在解释器启动时处理，
不受"用什么方式启动"影响（uvicorn / python -c / 子进程都能看到）；
环境变量在某些启动路径下会被覆盖或丢失。
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

#: builder 层的安装前缀（与 Dockerfile 里的 `pip install --prefix=` 对应）
PREFIX = Path("/install")


def main() -> int:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    target = PREFIX / "lib" / f"python{ver}" / "site-packages"

    if not target.is_dir():
        # 明确报错而不是静默跳过：构建期就该炸，别留到运行期
        print(f"[build] 预期目录不存在：{target}", file=sys.stderr)
        print("[build] 请检查 builder 阶段的 pip install --prefix 是否成功",
              file=sys.stderr)
        return 1

    site_dir = Path(sysconfig.get_paths()["purelib"])
    site_dir.mkdir(parents=True, exist_ok=True)
    pth = site_dir / "zz-videolens-install.pth"
    # 前缀 zz- 让它在其他 .pth 之后处理，避免被同名文件抢先
    pth.write_text(str(target) + "\n", encoding="utf-8")
    print(f"[build] linked {target} -> {pth}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
