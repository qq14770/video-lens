"""静态校验：JS 语法（若 node 可用）+ HTML/JS 的 DOM id 对齐 + 模板引用完整性。"""
import re
import pathlib

html = pathlib.Path("frontend/index.html").read_text(encoding="utf-8")
js = pathlib.Path("frontend/js/app.js").read_text(encoding="utf-8")
css = pathlib.Path("frontend/css/style.css").read_text(encoding="utf-8")
panels_p = pathlib.Path("frontend/js/panels.js")
panels = panels_p.read_text(encoding="utf-8") if panels_p.is_file() else ""
# 两个脚本共用一个 DOM，校验时合并起来看，避免把 panels.js 的引用误报为缺失
js_all = js + "\n" + panels

ids = set(re.findall(r"\bid=\"([\w-]+)\"", html))
# JS 通过 $('xxx') 简写访问 DOM（内部仍是 getElementById）。
# 合并两个脚本一起校验：它们共享同一个 DOM，分开看会把彼此引用的 id 误报为缺失。
used = set(re.findall(r"\$\(\s*[\"']([\w-]+)[\"']\s*\)", js_all)) | set(
    re.findall(r"getElementById\(\s*[\"']([\w-]+)[\"']\s*\)", js_all))
missing = sorted(u for u in used if u not in ids)
print("HTML 中的 id 数量:", len(ids))
print("JS 引用的 id 总数:", len(used))
print("JS 引用但 HTML 缺失的 id:", missing or "NONE ✓")

# 模板
tpl_used = sorted(set(re.findall(r"\$\(\s*[\"']((?:[\w-]*tpl[\w-]*))[\"']\s*\)", js_all)))
print("JS 引用的模板:", tpl_used, "| 全部存在:", all(t in ids for t in tpl_used))

# querySelector 的 class 钩子是否在 HTML 或模板里出现
cls_sel = set(re.findall(r"querySelector(?:All)?\(\s*[\"']\.([\w-]+)", js_all))
absent_cls = sorted(c for c in cls_sel if c not in html)
print("querySelector 类钩子缺失:", absent_cls or "NONE ✓")

# data-* 属性钩子对齐
data_html = set(re.findall(r"(data-[\w-]+)", html))
data_js = set(re.findall(r"(data-[\w-]+)", js_all))
orphan_js = sorted(d for d in data_js if d not in html and d not in data_html and "-" in d and not d.startswith("data-"))
print("HTML data 钩子:", len(data_html), "| JS 里用到但 HTML 没有的:", orphan_js or "NONE ✓")

# ---------------------------------------------------------------------------
# 括号平衡（仅对 CSS 做）。
#
# 为什么不对 JS 做：JS 里字符串/正则/模板插值都含括号，朴素计数必然误报。
# 踩过的坑：`/[.,;:，。；、）)】》」'"]+$/` 的字符类里有个半角 `)`，被判成"不平衡"；
# 而给 JS 写"跳过字面量"的过滤器又会吃掉模板串 ${} 里的真括号。与其维护一个
# 半吊子的 JS 词法器，不如把语法校验完全交给 node --check（权威且零误报），
# 这里只保留对 CSS 的括号检查（CSS 没有正则字面量，计数是可靠的）。
# ---------------------------------------------------------------------------
print("css: braces", css.count("{") == css.count("}"),
      ", parens", css.count("(") == css.count(")"))

# style.css 是否定义了 JS 里 toggle 的关键 class
toggled = set(re.findall(r"classList\.\w+\(\s*[\"']([\w-]+)[\"']", js_all))
undef = sorted(t for t in toggled if f".{t}" not in css and t not in html)
print("JS 里操作但 CSS/HTML 未定义的 class:", undef or "NONE ✓")

# 四态机依赖 hidden 属性做互斥显示。CSS 里若给元素写了作者级 display（flex/grid/…），
# 会盖掉 UA 的 [hidden]{display:none}，导致空态/加载/错误/结果同屏。
# 因此 style.css 必须存在全局复位：[hidden] { display: none !important; }
has_reset = bool(re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none[^}]*!important", css))
print("CSS 的 [hidden] 全局复位:", "存在 ✓" if has_reset else "缺失 ✗（四态会同屏）")

# 依赖 hidden 的节点，若同时有作者级 display 规则，必须靠上面这条复位兜住
hidden_nodes = re.findall(r'class="([\w\- ]+)"[^>]*(?<!aria-)\bhidden\b', html)
risky = sorted({c for grp in hidden_nodes for c in grp.split()
                if re.search(r"\." + re.escape(c) + r"[^\w-][^}]*display\s*:", css)})
print("带 hidden 且有作者级 display 的 class（需全局复位兜底）:", risky or "NONE")
if not has_reset and risky:
    print("!! 上述元素在当前 CSS 下无法被隐藏，请补 [hidden]{display:none!important}")

# ---------------------------------------------------------------------------
# 「下载 MP4」按钮的状态机：idle（发起合并）→ busy（合并中）→ ready（保存产物）。
# 这里断言三个易回归的点 —— 它们各自都出过真 bug：
#   1. click 处理器若**无条件** preventDefault，ready 态的 <a href download> 原生下载
#      会被拦掉，表现为「点了保存 MP4 没反应」；
#   2. 模板自带 target="_blank"，会让下载变成"开新标签"而不是保存文件，三态都要清掉；
#   3. busy 态必须不可点，否则会重复提交合并任务。
# ---------------------------------------------------------------------------
lines = js.splitlines()
blk, inside = [], False
for ln in lines:
    if 'openBtn.addEventListener("click"' in ln:
        inside = True
    if inside:
        blk.append(ln)
        if ln.strip() == "});":
            break
code = "\n".join(l for l in blk if not l.strip().startswith("//"))   # 注释里也含关键词

guard = code.find('dataset.mode !== "idle"')
pdef = code.find("preventDefault")
print("下载按钮 click 先判态再 preventDefault:",
      "✓" if 0 <= guard < pdef else "✗（ready 态会被拦成「没反应」）")
print("ready 态写入 href + download:", "✓" if (
    'btn.href = "/api/remux/"' in js and 'setAttribute("download", name)' in js) else "✗")
_target_clears = js.count('removeAttribute("target")')
print("三态均清掉 target=_blank:", "✓" if _target_clears == 3 else f"✗（只清了 {_target_clears} 处）")
_busy = css[css.index('.vl-btn--open[data-mode="busy"]'):][:220] if 'data-mode="busy"' in css else ""
_ready = css[css.index('.vl-btn--open[data-mode="ready"]'):][:160] if 'data-mode="ready"' in css else ""
print("busy 不可重复点击 / ready 可点击:",
      "✓" if ("pointer-events: none" in _busy and "pointer-events: none" not in _ready) else "✗")
