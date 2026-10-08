"""页面兜底解析：yt-dlp 没有提取器的站点，直接从 HTML 里取标题与播放直链。

**为什么需要这个模块**

yt-dlp 覆盖上千站点，但大量中小站点（尤其是聚合/自建站）没有对应提取器，
``extract_info`` 只会抛 ``Unsupported URL``。可这类站的播放器往往是把地址
**明文写在页面里**的，例如：

.. code-block:: text

    "video":{"url":"https:\/\/hls.example.cn\/videos5\/352c...\/352c....m3u8?auth_key=...",
             "type":"hls","thumbnails":null}

于是"不支持"并不等于"取不到"。本模块在 yt-dlp 失败后兜一道：

1. 取回页面 HTML（走 ``security.check_url`` 的 SSRF 校验，限制体积与超时）；
2. 从 ``og:title`` / ``<title>`` / ``<h1>`` 里取**真实标题**（这是用户最直接的诉求：
   粘视频页链接要能显示标题，而不是那串 URL 哈希或占位文案）；
3. 从 HTML 里扫出 m3u8 / mp4 直链（含 JSON 转义形态），交给上层当作可播源。

设计约束：
  * 只做**只读**抓取，绝不执行页面脚本、不跟随 playbook；
  * 体积上限 ``SCRAPE_MAX_BYTES``，防止踩到超大页面把内存吃满；
  * 全程 best-effort —— 任何一步失败都只是"没有兜底结果"，不改变原有报错行为。
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .config import config
from .security import check_url

log = logging.getLogger("videolens.scraper")

#: 通用浏览器 UA。部分站点对非浏览器 UA 直接返回空壳页。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# 页面抓取
# ---------------------------------------------------------------------------


def fetch_page(url: str, referer: str | None = None) -> str | None:
    """取回页面 HTML 文本；失败返回 ``None``（调用方按"没有兜底"处理）。

    ``url`` 必须已经过 SSRF 校验（本函数内部再校验一次，纵深防御）。
    """
    try:
        check_url(url)
    except Exception:                     # noqa: BLE001 —— 校验失败即放弃兜底
        return None

    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    # 超时取「解析看门狗」与 15s 的较小值：兜底不许把整个任务拖超时
    timeout = min(config.EXTRACT_TIMEOUT, 15)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers=headers, verify=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text" not in ctype and ctype:
                # 直链（视频/图片）没有可解析的标题，直接放弃
                return None
            raw = resp.content[: config.SCRAPE_MAX_BYTES]
    except Exception as exc:              # noqa: BLE001 —— 兜底路径不抛错
        log.debug("fetch_page failed for %s: %s", url, exc)
        return None

    # 优先按声明编码解码，其次按 UTF-8 容错，最后退回 gbk（部分中文老站）
    for enc in _encoding_candidates(resp, raw):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _encoding_candidates(resp: httpx.Response, raw: bytes) -> list[str]:
    """按「HTTP 头 → HTML meta → 常见中文编码」给出解码候选顺序。"""
    cands: list[str] = []
    if resp.encoding and resp.encoding.lower() != "utf-8":
        cands.append(resp.encoding)
    head = raw[:2048].decode("ascii", errors="ignore")
    meta = re.search(r'charset\s*=\s*["\']?([\w-]+)', head, re.I)
    if meta:
        cands.append(meta.group(1))
    cands += ["utf-8", "gb18030"]
    seen, out = set(), []
    for c in cands:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 标题提取
# ---------------------------------------------------------------------------


def page_title(page: str) -> str:
    """从 HTML 里取最像"视频标题"的文本，取不到返回空串。

    优先级（从最可靠往下退）：

      1. ``og:title`` / ``twitter:title`` —— 各站通用的社交卡片标题；
      2. ``itemprop="headline"`` —— Schema.org 微数据。**WordPress 主题（含本类
         聚合站）普遍用 ``<meta itemprop="headline">`` + ``<h1 class="post-title"
         itemprop="name headline">`` 标注正文标题**，而且常常压根没有 og:title，
         漏掉这一档就会白跑一趟兜底（这是实际踩到的坑）；
      3. ``<h1>`` / ``.post-title`` / ``.entry-title`` —— 正文标题节点；
      4. ``<title>`` —— 放最后，因为它几乎总带站点后缀，需要清洗。
    """
    if not page:
        return ""

    # ① 社交卡片
    for pattern in (
        r'<meta[^>]+property\s*=\s*["\']og:title["\'][^>]*content\s*=\s*["\']([^"\']+)',
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*property\s*=\s*["\']og:title',
        r'<meta[^>]+name\s*=\s*["\']twitter:title["\'][^>]*content\s*=\s*["\']([^"\']+)',
    ):
        m = re.search(pattern, page, re.I | re.S)
        if m:
            title = _clean(m.group(1))
            if title:
                return title

    # ② Schema.org 微数据：<meta itemprop="headline" content="...">
    #    属性顺序不固定，正反两种写法都要试
    for pattern in (
        r'<meta[^>]+itemprop\s*=\s*["\']headline["\'][^>]*content\s*=\s*["\']([^"\']+)',
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*itemprop\s*=\s*["\']headline',
    ):
        m = re.search(pattern, page, re.I | re.S)
        if m:
            title = _clean(m.group(1))
            if title:
                return title

    # ③ 正文标题节点：先按 class 精确命中（主题常见命名），再退回裸 <h1>
    for pattern in (
        r'<h[12][^>]*class\s*=\s*["\'][^"\']*(?:post-title|entry-title|article-title)[^"\']*["\'][^>]*>(.*?)</h[12]>',
        r'<h[12][^>]*itemprop\s*=\s*["\'][^"\']*headline[^"\']*["\'][^>]*>(.*?)</h[12]>',
        r"<h1[^>]*>(.*?)</h1>",
    ):
        m = re.search(pattern, page, re.I | re.S)
        if m:
            title = _clean(m.group(1))
            if title:
                return title

    # ④ 兜底：<title>（带站点后缀，需清洗）
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if m:
        return _strip_site_suffix(_clean(m.group(1)))
    return ""


def _clean(text: str) -> str:
    """去标签 → 解实体 → 压空白。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


#: 看起来像「标题的一部分」而不是站点名的尾巴 —— 命中就不裁剪。
#: 例："某部纪录片 - 第二季"、"某某 - 上集" 里的尾巴是内容本身，砍掉就错了。
_TITLE_TAIL_WORDS = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百]+"
    r"|[上下中]集|前篇|后篇|完结|预告|花絮|正片|番外|特辑"
    r"|EP?\s*\d+|Part\s*\d+|Vol\.?\s*\d+|S\d+E\d+"
    r"|\d{4}\s*年?|高清|超清|蓝光|完整版|未删减)",
    re.I,
)


def _strip_site_suffix(title: str) -> str:
    """去掉 ``标题 - 站点名`` / ``标题_站点名`` / ``标题 | 站点名`` 这类尾巴。

    只有同时满足下面三条才裁剪（避免把正常标题砍坏）：

      1. 分隔符两边的长度都合理（主体 ≥4 字、尾巴 ≤20 字）；
      2. 尾巴不像「内容本身」—— 命中 :data:`_TITLE_TAIL_WORDS`（第二季/上集/EP12…）就保留；
      3. 尾巴看起来像站点标识：含域名点号、常见站点后缀词，或整体是短英文/拼音串。

    宁可少裁一刀（标题多带个站名）也不能砍掉真正的标题词。
    """
    for sep in (" - ", " – ", " — ", " | ", " _ ", "｜", "丨"):
        if sep not in title:
            continue
        head, _, tail = title.rpartition(sep)
        head, tail = head.strip(), tail.strip()
        if not head or not tail or len(head) < 4 or len(tail) > 20:
            continue
        if _TITLE_TAIL_WORDS.match(tail):        # 「第二季」这类属于标题本体
            continue
        looks_like_site = (
            "." in tail                                          # www.xx.com / xx.com
            or re.search(r"(?:网|站|官网|首页|视频|影视|影院|TV|视频网)$", tail, re.I)
            or re.fullmatch(r"[A-Za-z0-9\-_.]{2,20}", tail)       # 纯英文/拼音站名
        )
        if looks_like_site:
            return head
    return title


# ---------------------------------------------------------------------------
# 直链提取
# ---------------------------------------------------------------------------

#: 命中即认为是一条可播的媒体直链。
#: 地址在页面里可能以多种形态出现，必须都能吃下：
#:   1. 正常：        https://cdn.xx/v/a.m3u8?auth_key=...
#:   2. 协议相对：    //cdn.xx/v/a.m3u8
#:   3. JSON 转义：   https:\/\/cdn.xx\/v\/a.m3u8?auth_key=...
#:   4. 双重转义：    https:\\/\\/cdn.xx\\/v\\/a.m3u8      ← 页面里嵌 JSON 字符串时最常见
#:
#: 写法要点（踩过坑）：主体用「排除式字符类」而不是精确匹配转义，
#: 否则 `\\` 在字符类里会被当成转义字符本身、把反斜杠也排除掉，导致整条匹配失败。
#: 这里改为：允许任意非空白/非引号/非尖括号的字符（**含反斜杠**）出现在路径里，
#: 只要它最终以 .m3u8/.mp4/... 结尾即可；随后由 :func:`_normalize` 还原成真 URL。
_MEDIA_RE = re.compile(
    r"""(?:https?:|\\{0,2}/\\{0,2}/)?[^\s"'<>]{6,700}?\.(?:m3u8|mp4|webm|flv)(?:\?[^\s"'<>]{0,400})?""",
    re.I,
)

#: 播放器常用键名：命中这些键的取值优先当作正片地址（而不是广告/预览）
_HINT_KEYS = ("video", "url", "src", "source", "file", "playurl", "play_url", "hls", "m3u8")

#: 广告位标记：地址紧邻这些字符串时几乎可以肯定是贴片/推荐位，而不是正片
_AD_MARKERS = ("ad_slot", "ad_id", "ads", "advert", "preroll", "pre-roll",
               "promo", "sponsor", "banner", "广告", "贴片")

#: 播放器配置里「同一视频的不同编码变体」的键名。这些是**同一个视频**的备选编码，
#: 不能当成两个视频；展示时取更通用的那一路即可。
_CODEC_VARIANT_RE = re.compile(
    r'"(?P<key>[a-z0-9_]*video[a-z0-9_]*)"\s*:\s*\{\s*"url"\s*:\s*"(?P<url>[^"]{10,600})"',
    re.I,
)

#: 判定两个 video* 键属于「同一个播放器配置块」的依据 —— 两者之间不能再出现
#: 这些"新块开始"的标志。用结构信号而不是字符距离：纯距离阈值在压缩过的页面上
#: 会误判（实测同一视频的变体键紧凑、不同播放器之间也只隔一两百字符）。
_BLOCK_BOUNDARY_RE = re.compile(
    r"<div|<section|<article|<script|new\s+\w*Player|dplayer|artplayer|"
    r"player-container|video-container|jwplayer|plyr",
    re.I,
)


#: 兜底距离上限：即使中间没有结构标志，隔得这么远也肯定不是同一个播放器块。
_SAME_PLAYER_GAP = 2000


def _in_same_block(page: str, prev_end: int, cur_start: int) -> bool:
    """两个 video* 键是否属于同一个播放器配置块。

    同一视频的编码变体是紧挨着写在一个对象里的
    （``{"video":{...},"video_h265":{...}}``），中间不会有新的 HTML 标签、
    新的 ``<script>``、也不会有新的播放器构造调用。只要中间出现了这些，
    就说明进入了下一个播放器 —— 那是另一个视频。
    """
    between = page[prev_end:cur_start]
    if _BLOCK_BOUNDARY_RE.search(between):
        return False
    return len(between) < _SAME_PLAYER_GAP


def page_media(page: str, base_url: str) -> list[str]:
    """扫出页面里的候选媒体直链，**按视频分组**后每个视频给一条代表地址。

    页面里一个视频常有多个编码变体，形如::

        {"video":      {"url":"https://cdn/a.m3u8"},      ← h264，通用性最好
         "video_h265": {"url":"https://cdn/b.m3u8"}}      ← h265，省带宽但兼容性差

    而一个页面又可能有多个播放器（多个视频）。所以要区分两件事：

      * **同一块内的多个键** = 同一个视频的编码变体 → 只保留一条（优先不带编码后缀的
        ``video``，即 h264，浏览器/ffmpeg 兼容性最好）；
      * **不同的配置块** = 不同的视频 → 各自保留一条，不能折叠。

    之前把「变体」误当成「重复源」去重，导致一个有两个视频的页面只显示出一个
    （用户实际反馈）。这里改为按配置块分组，块内选主变体。

    兜底：如果页面里根本没有 ``video`` 这类结构化配置（普通 ``<video src>``、
    独立 mp4 链接等），就退回全文正则扫描，并按"更像正片"的顺序排序。
    """
    if not page:
        return []

    # ---- ① 结构化优先：按「播放器配置块」分组 ----
    # 同一块内的多个 video* 键 = 同一视频的编码变体；块与块之间 = 不同视频。
    # 块的切分依据：相邻 video 键的间距。同一播放器配置里各变体键是紧挨着的
    # （实测 <100 字符），而两个播放器之间隔着整段播放器初始化代码（>400 字符）。
    blocks: list[list[tuple[int, str, int]]] = []   # [[(变体优先级, url, 位置), ...]]
    for m in _CODEC_VARIANT_RE.finditer(page):
        url = _normalize(m.group("url"))
        if not url:
            continue
        key = m.group("key").lower()
        # 变体优先级：video 本身最优先（h264，通用性最好），
        # video_h264 显式声明次之，其余编码后缀（h265/av1…）最后。
        if key == "video":
            vrank = 0
        elif "264" in key:
            vrank = 1
        else:
            vrank = 2
        if blocks and _in_same_block(page, blocks[-1][-1][2], m.start()):
            blocks[-1].append((vrank, url, m.start()))
        else:
            blocks.append([(vrank, url, m.start())])

    if blocks:
        out: list[str] = []
        seen: set[str] = set()
        for variants in blocks:
            variants.sort(key=lambda t: t[0])       # 块内取兼容性最好的变体
            url = variants[0][1]
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    # ---- ② 退回全文扫描（无结构化配置的普通页面）----
    scored: list[tuple[int, int, str]] = []
    for idx, m in enumerate(_MEDIA_RE.finditer(page)):
        url = _normalize(m.group(0))
        if not url:
            continue
        ctx = page[max(0, m.start() - 200): m.start()].lower()
        prio = 2
        if re.search(r'"(?:video|vod|player|source)"\s*:\s*\{[^}]{0,80}$', ctx):
            prio = 0
        elif any(k in ctx for k in _HINT_KEYS):
            prio = 1
        if any(marker in ctx for marker in _AD_MARKERS):
            prio += 5
        if re.search(r"(?:auth_key|token|sign|expire)=", url, re.I):
            prio -= 1
        scored.append((prio, idx, url))

    seen, out = set(), []
    for _, _, url in sorted(scored, key=lambda t: (t[0], t[1])):
        absolute = urljoin(base_url, url)
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


def _normalize(url: str) -> str:
    """把页面里各种转义形态还原成正常 URL；不合法则返回空串。

    输入可能长这样（都是真实见过的形态）::

        https://cdn.xx/v/a.m3u8?auth_key=1
        https:\\/\\/cdn.xx\\/v\\/a.m3u8      ← JSON 里的双重转义
        https:\\u002F\\u002Fcdn.xx\\/a.m3u8  ← \\uXXXX 转义

    处理顺序：先解 ``\\uXXXX``，再把所有反斜杠转义（``\\/``、``\\:`` 等）还原，
    最后统一去掉可能残留的引号与首尾空白。
    """
    url = (url or "").strip().strip("\"'")

    # ① \uXXXX 形式的转义（如 \u002F = '/'、\u0026 = '&'）
    def _un_uuid(m: "re.Match[str]") -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    url = re.sub(r"\\u([0-9a-fA-F]{4})", _un_uuid, url)

    # ② 反斜杠转义：JSON 里 `\/` 与双重转义的 `\\/` 都要还原。
    #    这里直接**删掉所有反斜杠**（而不是 `\\(.)` → `\1` 替换一次），
    #    因为 URL 里合法的反斜杠本来就不存在，转义层数可能不止一层：
    #    `\\/` 用单次替换只会得到 `\/`，仍然不是合法 URL（踩过这个坑）。
    url = url.replace("\\", "")

    # ③ HTML 实体（&amp; 出现在查询串里很常见）
    url = html.unescape(url)

    # ④ 去掉从 JSON/JS 里粘来的收尾标点（正则为了贪心会带上 , ; ) } 之类）
    url = url.rstrip(".,;:!?)}]'\" \t\r\n")

    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url, re.I):
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if not parsed.netloc or "." not in parsed.netloc:
        return ""
    return url


# ---------------------------------------------------------------------------
# 组合入口
# ---------------------------------------------------------------------------


def scrape(url: str) -> dict[str, Any]:
    """兜底解析入口：返回 ``{"title": str, "media": [str, ...]}``。

    两者都可能为空 —— 调用方据此决定是"仍有标题可用"还是"彻底失败"。
    """
    page = fetch_page(url)
    if not page:
        return {"title": "", "media": []}

    title = page_title(page)
    media = page_media(page, url)

    # 有些站把播放配置塞在 <script> 的 JSON 里且做了 HTML 实体转义，
    # 上面那轮可能漏掉；解一次实体再扫一遍作为补充。
    if not media:
        media = page_media(html.unescape(page), url)

    if title or media:
        log.info("scraper fallback: title=%r media=%d", title[:40], len(media))
    return {"title": title, "media": media}


def looks_like_json_config(page: str) -> bool:
    """页面里是否含类 JSON 的播放配置（仅用于日志诊断）。"""
    try:
        json.loads(page[:1])
    except Exception:                     # noqa: BLE001
        return False
    return True
