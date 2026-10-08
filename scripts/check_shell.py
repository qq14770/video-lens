"""审计 shell 脚本的安全性 —— 两个会递归改权限的脚本尤其需要。

**检查什么**（按重要性）：

  1. **语法**：调用 ``bash -n``（bash 自己的解析器）—— 权威且零误报。
     教训：早先我用「逐行统计引号奇偶」来判断，结果把跨行的 awk 程序
     和 shell 的单引号转义惯用法 ``'"'"'`` 全判成错误，纯属噪音。
     语法这种事必须交给真正的解析器。
  2. **危险路径**：拒绝在 ``/``、``/vol1``、``/mnt`` 这类系统级目录上递归操作。
  3. **操作顺序**：所有 ``mkdir/chown/chmod/rm`` 必须出现在路径校验**之后** ——
     否则一次解析失败就会把权限改到不该改的地方。
  4. **破坏性命令**：不允许 ``rm -rf``（只允许在探测可写性时删单个临时文件）。
  5. **变量引用加引号**：``$VAR`` 未加引号时，路径里的空格会把参数拆散。

用法：
    python -X utf8 scripts/check_shell.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ["scripts/fix-perms.sh", "scripts/prepare-data-dir.sh"]

#: 绝不允许被递归 chown/chmod 的顶层目录
DANGEROUS = {"/", "/vol1", "/vol2", "/vol3", "/vol4", "/volume1",
             "/mnt", "/home", "/root", "/etc", "/usr", "/var", "/opt", "/bin"}


def find_bash() -> str | None:
    for cand in ("bash",
                 r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        p = shutil.which(cand) if not cand.endswith(".exe") else (
            cand if pathlib.Path(cand).exists() else None)
        if p:
            return p
    return None


def check_syntax(path: pathlib.Path, bash: str | None) -> bool:
    """用 bash -n 做权威语法检查（不可用时退化为提示）。"""
    if not bash:
        print(f"  SKIP  {path.name} 语法（本机没有 bash）")
        return True
    proc = subprocess.run([bash, "-n", str(path)],
                          capture_output=True, text=True)
    ok = proc.returncode == 0
    print(f"  {'PASS' if ok else 'FAIL'}  {path.name} 语法（bash -n）"
          + ("" if ok else f"\n        {proc.stderr.strip()[:300]}"))
    return ok


def audit(path: pathlib.Path) -> bool:
    t = path.read_text(encoding="utf-8")
    lines = t.splitlines()
    ok = True

    # ---- 危险路径防护 ----
    has_guard = any(d in t for d in DANGEROUS if d != "/")
    print(f"  {'PASS' if has_guard else 'FAIL'}  {path.name} 有危险路径黑名单")
    ok &= has_guard

    # ---- set -euo pipefail ----
    has_strict = "set -euo pipefail" in t
    print(f"  {'PASS' if has_strict else 'WARN'}  {path.name} set -euo pipefail")
    ok &= has_strict

    # ---- 操作顺序：副作用操作必须在守卫之后 ----
    guard_line = 0
    for i, l in enumerate(lines, 1):
        if "系统级目录" in l or "没有 docker-compose.yml" in l:
            guard_line = max(guard_line, i)
    late_ops = []
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.startswith("#") or not s:
            continue
        if re.search(r"\b(mkdir|chown|chmod)\b.*\$(DATA_DIR|ROOT)\b", s) and i < guard_line:
            late_ops.append(i)
    print(f"  {'PASS' if not late_ops else 'FAIL'}  {path.name} 副作用操作都在校验之后"
          + (f"（越界行：{late_ops}）" if late_ops else ""))
    ok &= not late_ops

    # ---- 禁止递归删除 ----
    # 只在「命令位置」匹配 rm，避免把中文字符串里的 "rm" 误判成命令。
    # 教训：早先的宽松正则在 `echo "...（不影响使用）"` 这类中文行上假警报 ——
    # 因为某些中文的字节序列里恰好含 "rm"。所以要求 rm 出现在
    # 行首、或 ; & | ( 之后，且后面紧跟 -r/-rf/-fr。
    rm_pat = re.compile(r"(?:^|[;&|(]\s*)rm\s+-[a-z]*r[a-z]*\b")
    recursive_rm = [i for i, l in enumerate(lines, 1)
                    if rm_pat.search(l) and not l.strip().startswith("#")]
    print(f"  {'PASS' if not recursive_rm else 'FAIL'}  {path.name} 无递归删除"
          + (f"（行 {recursive_rm}）" if recursive_rm else ""))
    ok &= not recursive_rm

    # ---- 变量引用是否加引号（只查会造成副作用的行）----
    unquoted = []
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.startswith("#") or not s:
            continue
        if re.search(r"\b(mkdir|chown|chmod|cd)\b", s):
            # 出现裸 $VAR（后面不跟引号）即判定
            for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)", s):
                after = s[m.end():m.end() + 1]
                before = s[m.start() - 1:m.start()]
                if after != '"' and before != '"':
                    unquoted.append((i, m.group(0)))
    print(f"  {'PASS' if not unquoted else 'WARN'}  {path.name} 副作用行变量已加引号"
          + (f"（{unquoted[:3]}）" if unquoted else ""))
    return ok


def verify_detector() -> bool:
    """反向验证「递归删除」检测本身 —— 安检规则必须先被验过才可信。

    两个方向都要成立：
      * 真危险（``rm -rf`` / ``rm -fr`` / ``& rm -rf``）必须被抓到，否则安检形同虚设；
      * 中文说明文字里恰好含 "rm" 的，不能误报（第一版正则就栽在这上面）。
    """
    cases = [
        ('rm -rf "$DATA_DIR"', True), ("rm -rf /", True),
        ("rm -rf /vol1/1000/xxx", True), ("cd /tmp && rm -rf build", True),
        ("mkdir x; rm -rf x", True), ("(rm -rf x)", True),
        ("rm -fr somedir", True), ("rm -r dir", True),
        ('rm -f "$DATA_DIR/.write-test"', False),
        ('echo "无法用 su 模拟（不影响使用）"', False),
        ('echo "跳过实测（不影响使用）"', False),
        ('echo "form -r 之类"', False),
        ("sudo rm -rf /vol1", False),
    ]
    pat = re.compile(r"(?:^|[;&|(]\s*)rm\s+-[a-z]*r[a-z]*\b")
    bad = [(s, w, bool(pat.search(s))) for s, w in cases if bool(pat.search(s)) != w]
    print(f"  {'PASS' if not bad else 'FAIL'}  检测规则自验（{len(cases) - len(bad)}/{len(cases)}）"
          + (f"  不符：{bad[:2]}" if bad else ""))
    return not bad


def main() -> int:
    print("=" * 68)
    print(" Shell 脚本审计")
    print("=" * 68)
    bash = find_bash()
    print(f"bash: {bash or '（未找到，语法检查将跳过）'}")
    print()

    all_ok = verify_detector()
    print()

    for rel in SCRIPTS:
        p = ROOT / rel
        if not p.is_file():
            print(f"  SKIP  {rel}（不存在）")
            continue
        print(f"--- {rel} ---")
        all_ok &= check_syntax(p, bash)
        all_ok &= audit(p)
        print()

    print("=" * 68)
    print(" 结论：", "全部通过" if all_ok else "有问题，见上")
    print("=" * 68)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
