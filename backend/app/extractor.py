"""核心解析层：yt-dlp 调用 + 广告/冗余过滤 + 播放源择优。

对外只暴露两个东西：
    :func:`extract`   —— 输入分享链接，返回**已经过白名单投影**的结果字典
    :class:`ExtractError` —— 面向用户的可读错误

「过滤广告与冗余元素」在本层的两条落点：

  1. 数据层白名单投影：yt-dlp 原始 info 有 100+ 字段（描述、标签、UP 主、互动数、
     推荐列表、弹幕接口、贴片广告清单……），本函数只投影 UI 需要的 5 类字段，
     其余一律不出后端。前端因此**结构上不可能**渲染出冗余信息。
  2. 流层过滤：丢弃贴片/前插广告流、DRM 加密流、音画缺失流、重复清晰度流，
     并对同一 (高度, 编码, 协议) 只保留码率最高的一条。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

from .config import config
from .providers import identify

log = logging.getLogger("video-lens.extractor")

#: 通用浏览器 UA：页面兜底解析时，回源 CDN 与页面请求都用它（部分站点对非浏览器 UA 返回空壳）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 解析失败时抛出的业务异常（message 直接给前端展示，因此必须是人类可读文案）
class ExtractError(RuntimeError):
    def __init__(self, message: str, code: str = "extract_failed") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 过滤规则
# ---------------------------------------------------------------------------

#: 疑似广告/贴片流的特征（命中 format_note 或 URL 即丢弃）
_AD_PATTERNS = re.compile(
    r"(?:^|[\s\-_/])(?:ad(?:vertis|s)?|pre-?roll|mid-?roll|bumper|sponsor|preroll|贴片|前插|广告)"
    r"(?:$|[\s\-_/.])",
    re.IGNORECASE,
)

#: 需要 DRM 才能解密的流：浏览器无法直接播放，展示出来只会误导用户
_DRM_KEYS = ("has_drm", "drm", "license_url")

#: 浏览器 <video> 能原生播放的封装/协议
_NATIVE_OK = {"mp4", "m4a", "mov", "webm", "ogg"}
_HLS_PROTOCOLS = {"m3u8", "m3u8_native", "hls"}
_DASH_PROTOCOLS = {"dash", "dash_manifest"}
_UNSUPPORTED_PROTOCOLS = {"ism", "f4m", "rtmp", "rtsp", "websocket"}

# 最多向前端展示几条清晰度（超出即按质量截断，避免列表变成「清晰度矩阵」噪音）
_MAX_SOURCES = 6


def _looks_ad(fmt: dict[str, Any]) -> bool:
    """判定一条流是否是贴片/广告流。"""
    for key in ("format_note", "format", "manifest_url"):
        val = fmt.get(key)
        if isinstance(val, str) and _AD_PATTERNS.search(val):
            return True
    url = fmt.get("url") or ""
    # 常见的广告 CDN 域名特征：ad./ads./.ad-server/ 之类
    return bool(re.search(r"(?:^|[/.)])(?:ads?|adserver[-.]|advert)", url, re.IGNORECASE))


def _has_drm(fmt: dict[str, Any]) -> bool:
    return any(bool(fmt.get(key)) for key in _DRM_KEYS)


def _height_of(fmt: dict[str, Any]) -> int:
    """稳定的高度取值：部分站点只在 ``resolution`` 里给 '1920x1080'。"""
    h = fmt.get("height")
    if isinstance(h, (int, float)) and h > 0:
        return int(h)
    m = re.search(r"(\d{3,5})", str(fmt.get("resolution") or ""))
    return int(m.group(1)) if m else 0


def _bitrate_of(fmt: dict[str, Any]) -> float:
    for key in ("tbr", "vbr", "abr", "bitrate"):
        val = fmt.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return 0.0


def _quality_label(height: int, fps: float | None) -> str:
    """把技术参数翻译成人话（2160p → 4K·超高清）。"""
    tiers = [
        (2160, "4K 超清"), (1440, "2K 高清"), (1080, "1080P 蓝光"),
        (720, "720P 准高清"), (480, "480P 标清"), (360, "360P 流畅"),
    ]
    for th, name in tiers:
        if height >= th:
            label = name
            break
    else:
        label = f"{height}P" if height else "自适应"
    if fps and fps >= 50 and height >= 1080:
        label += f"·{int(fps)}帧"
    return label


def _container_of(fmt: dict[str, Any]) -> str:
    ext = (fmt.get("ext") or "").lower()
    if ext in ("m3u8",):
        return "m3u8"
    return ext or (
        "m4s" if fmt.get("protocol") in _DASH_PROTOCOLS else "mp4"
    )


def _codec_label(fmt: dict[str, Any]) -> str:
    """视频+音频编码简述，用于让用户判断播放兼容性。"""
    v = (fmt.get("vcodec") or "").lower()
    a = (fmt.get("acodec") or "").lower()
    v = "" if v in ("none", "") else v.split(".")[0]
    a = "" if a in ("none", "") else a.split(".")[0]
    parts = [p for p in (v.upper(), a.upper()) if p]
    return "/".join(parts[:2])


def _quality_text(fmt: dict[str, Any]) -> str:
    """把「清晰度 · 编码」拼成一行标签；缺字段的站点也能给出像样的名字。"""
    label = _quality_label(_height_of(fmt), fmt.get("fps"))
    codec = _codec_label(fmt)
    return f"{label} · {codec}" if codec else label


def _is_playable(fmt: dict[str, Any]) -> bool:
    """浏览器可行性判定：封装格式 + 传输协议 + 是否有画面。

    三条分支的取舍：

      * **HLS**：直接放行，交给 hls.js / Safari 原生播放。
      * **DASH 分片**（``https_dash`` 等）：只有「音画同轨」的才可用。单轨分片
        （纯视频 m4s 或纯音频 m4s）在浏览器里拼不出完整视频，展示给用户只会造成
        「点了没声音 / 有声音没画面」的困惑，因此丢弃。
      * **渐进式单文件**（mp4/webm/mov…）：通用提取器（直接粘贴 .mp4 链接的场景）
        不做 ffprobe，``vcodec/acodec`` 会是 ``None`` —— 这种单文件流天然音画同轨，
        必须放行；只有明确标注 ``none`` 的才按「无画面/纯音频」剔除。
    """
    ext = _container_of(fmt)
    proto = (fmt.get("protocol") or "").lower()
    if proto in _UNSUPPORTED_PROTOCOLS:
        return False
    if proto in _HLS_PROTOCOLS:
        return True                       # 交给 hls.js

    def declared(v: Any) -> bool:         # 站点明确上报了编码
        return isinstance(v, str) and v.lower() not in ("", "none")

    def declared_none(v: Any) -> bool:    # 站点明确上报「这一路没有」
        return isinstance(v, str) and v.lower() == "none"

    vc, ac = fmt.get("vcodec"), fmt.get("acodec")
    if declared_none(vc):
        return False                      # 纯音频轨，不是「视频」
    if proto in _DASH_PROTOCOLS:
        # DASH 只保留音画同轨的整片流
        return declared(vc) and declared(ac) and ext in _NATIVE_OK | {"mp4", "webm", "m4s"}
    return ext in _NATIVE_OK              # 渐进式：容器能被 <video> 原生解码即可


def proxy_path(fmt: dict[str, Any]) -> str:
    """生成本页播放器使用的代理播放地址。

    之所以不把原始直链直接丢给 <video>：
      - 跨域：站点 CDN 不下发 CORS 头，前端拿不到数据；
      - 防盗链：B 站/抖音校验 ``Referer`` + ``User-Agent``，而浏览器禁止 JS 设置这两个头；
      - 混合内容：直链常为 ``http://``，在 https 页面里会被浏览器直接拦掉。
    因此这里把 yt-dlp 采集到的请求头交给代理层的 :class:`~app.stream_proxy.HeaderJar`
    暂存（不进 URL、不进前端），只把签名后的相对路径返回给前端。
    """
    from .stream_proxy import play_url

    return play_url(fmt["url"], fmt.get("http_headers"))


# ---------------------------------------------------------------------------
# 解析主流程
# ---------------------------------------------------------------------------

def _build_ydl_opts() -> dict[str, Any]:
    """yt-dlp 参数：只解析不下载，短超时，可选浏览器指纹与登录 Cookie。"""
    opts: dict[str, Any] = {
        # —— 只要元数据 ——
        "skip_download": True,
        "noplaylist": True,          # 粘贴合集链接时取首个分 P，而不是把整张列表拖回来
        "extract_flat": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,        # 单个分 P 失败不影响整体
        # —— 网络健壮性 ——
        "socket_timeout": min(config.EXTRACT_TIMEOUT, 20),
        "extractor_retries": 1,
        "fragment_retries": 1,
        "http_chunk_size": None,
        # —— 让 HLS/DASH 直链保持原样返回 ——
        # —— 元数据要全，但绝不下载 ——
        # "youtube": "dash"：让 YouTube 返回「音画同轨」的 mp4 流。
        # 默认的 DASH 是音视频分离的两条流，浏览器 <video> 无法直接播，
        # 而本项目的定位是「提取到能直接用的视频直链」，因此优先 muxed 封装。
        "format": "bv*+ba/b",
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    # 可选：安装 curl_cffi 后开启 TLS 指纹，可解掉一部分「412/风控」站点
    if config.IMPERSONATE:
        try:
            opts["impersonate"] = config.IMPERSONATE
        except Exception:            # noqa: BLE001 —— 老版本 yt-dlp 不认识该键
            opts.pop("impersonate", None)
    # 可选：VX_COOKIES_FILE=/path/cookies.txt 时读取 Netscape 格式 Cookie，
    # 用于 B 站大会员 1080P/4K、付费课程等需要登录态的清晰度。
    cookies = os.getenv("VX_COOKIES_FILE", "").strip()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _first_entry(info: dict[str, Any]) -> dict[str, Any]:
    """playlist → 取第一个真正的视频条目。"""
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            raise ExtractError("该链接是一个没有可播放条目的合集/列表", "empty_playlist")
        return entries[0]
    return info


class _YdlLogger:
    """收集 yt-dlp 的错误行。

    ``ignoreerrors=True`` 让单个分 P 失败不影响整体，代价是失败原因被丢掉（``extract_info``
    直接返回 ``None``）。这里把 ``error()`` 的原文捞回来，用于生成可读的失败文案 ——
    否则用户只会看到"站点无响应"这种毫无下一步的提示。
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:            # yt-dlp 把 info 也走 debug，忽略
        return None

    def info(self, msg: str) -> None:
        return None

    def warning(self, msg: str) -> None:
        return None

    def error(self, msg: str) -> None:
        if msg and len(self.errors) < 8:
            self.errors.append(str(msg))

    def latest(self) -> str:
        """最近一条错误原文（yt-dlp 的错误通常最后一条最贴近根因）。"""
        return self.errors[-1] if self.errors else ""


#: 站点侧错误关键词 → (错误码, 中文可读文案)。顺序敏感：越具体的放越前面。
_ERROR_HINTS: tuple[tuple[str, str, str], ...] = (
    (r"Unsupported URL", "unsupported", "该站点暂不支持解析（yt-dlp 无对应提取器）"),
    (r"Private video", "private", "该视频为私密/仅自己可见"),
    (r"Video is not (?:longer )?available|was removed|视频已删除", "gone", "视频已被删除或下架"),
    (r"age[^\n]{0,40}confirm|age_required|年龄验证", "age_gate",
     "该视频需要年龄验证，请配置 VX_COOKIES_FILE 后重试"),
    (r"(?:cookies? (?:required|needed)|login required|需要登录|仅.{0,4}(?:登录|大会员).{0,6}可见)", "login_required",
     "该内容需要登录：请用浏览器登录站点后导出 cookies.txt，并设置环境变量 VX_COOKIES_FILE"),
    (r"(?:412|403|Forbidden|Request blocked|风控|验证|slider|captcha)", "blocked",
     "被站点风控拦截：请稍后重试，或设置 VX_IMPERSONATE=chrome（需先 pip install curl_cffi）"),
    (r"(?:timed? out|timeout)", "timeout", "站点响应超时：请确认本机可直接访问该站点（未走代理/未被拦截）"),
    (r"(?:getaddrinfo failed|Name or service not known|nodename)", "dns",
     "域名解析失败：请检查链接是否正确、本机 DNS 是否可用"),
    (r"(?:SSL|certificate|cert verify)", "tls",
     "证书校验失败：本机系统时间或网络代理可能异常"),
)


def classify_error(message: str) -> tuple[str, str]:
    """把 yt-dlp 的英文报错翻译成「错误码 + 下一步建议」。

    命中已知特征就给标准文案；否则保留压缩后的原文片段，方便排查但不整段倾倒。
    """
    for pattern, code, friendly in _ERROR_HINTS:
        if re.search(pattern, message, re.IGNORECASE):
            return code, friendly
    brief = re.sub(r"\s+", " ", message or "").strip()
    return "extract_failed", "解析失败：" + (brief[:150] or "站点可能已改版，或视频已删除")


def _pick_sources(raw_formats: list[dict[str, Any]],
                  duration: Any = None) -> list[dict[str, Any]]:
    """流过滤 → 去重 → 排序 → 截断，输出前端可直接渲染的 sources 列表。

    ``duration`` 只为给 HLS 源估算体积（清单里没有 filesize，需要"码率 × 时长"）。
    """
    candidates: list[dict[str, Any]] = []
    dropped = {"ad": 0, "drm": 0, "unplayable": 0, "empty": 0}

    for fmt in raw_formats:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            dropped["empty"] += 1
            continue
        if _looks_ad(fmt):                       # ① 贴片/前插广告流
            dropped["ad"] += 1
            continue
        if _has_drm(fmt):                        # ② DRM 加密流
            dropped["drm"] += 1
            continue
        if not _is_playable(fmt):                # ③ 单轨/不支持的协议
            dropped["unplayable"] += 1
            continue
        candidates.append(fmt)

    # ④ 同一 (高度, 编码, 封装) 只留码率最高的一条 —— 去掉「同清晰度重复源」
    #    注意带上 _group：页面兜底解析时，**同一个页面里的多个视频**元信息全为空，
    #    若不加分组标识就会被当成"同清晰度的重复源"折叠掉，用户只能看到一个视频
    #    （实际反馈过的问题）。同一个视频的多个清晰度仍按原规则去重。
    best: dict[tuple[int, str, str, int], dict[str, Any]] = {}
    for fmt in candidates:
        key = (_height_of(fmt), (fmt.get("vcodec") or "none").split(".")[0],
               _container_of(fmt), int(fmt.get("_group") or 0))
        cur = best.get(key)
        if cur is None or _bitrate_of(fmt) > _bitrate_of(cur):
            best[key] = fmt

    ordered = sorted(
        best.values(),
        key=lambda f: (_height_of(f), _bitrate_of(f), f.get("fps") or 0),
        reverse=True,
    )[:_MAX_SOURCES]

    log.debug("dropped formats: %s", dropped)

    # 一个页面兜底解析出多个视频时，各源元信息全空、标签会一模一样（"自适应"），
    # 用户根本分不清哪条是哪个视频。这里给它们加序号，让列表自解释。
    groups = [g for g in (int(f.get("_group") or 0) for f in ordered)]
    multi_video = len(set(groups)) > 1 if groups else False

    sources: list[dict[str, Any]] = []
    for fmt in ordered:
        height = _height_of(fmt)
        proto = (fmt.get("protocol") or "").lower()
        is_hls = proto in _HLS_PROTOCOLS
        label = _quality_text(fmt)
        if multi_video:
            # ordered 是按清晰度降序，不是按页面顺序；用 _group 还原"第几个视频"
            label = f"视频 {int(fmt.get('_group') or 0) + 1} · {label}"
        sources.append({
            "format_id": fmt.get("format_id"),
            "label": label,
            "height": height,
            "fps": fmt.get("fps"),
            "ext": _container_of(fmt),
            "protocol": "hls" if is_hls else ("dash" if proto in _DASH_PROTOCOLS else proto or "http"),
            "is_hls": is_hls,
            "bitrate_kbps": round(_bitrate_of(fmt)) or None,
            "size_bytes": fmt.get("filesize") or fmt.get("filesize_approx"),
            # HLS 源 yt-dlp 通常不给 filesize（清单里本来就没有），
            # 用「声明码率 × 总时长」补一个预估值，前端就能在下载**之前**告诉用户多大。
            # 实测与真实产物误差 ~3%；估算值单独一个字段，不与确定值混淆。
            "size_estimate_bytes": _estimate_size(fmt, duration, is_hls),
            "direct_url": fmt["url"],            # 用户要的「视频直链」原文
            "play_url": proxy_path(fmt),         # 本页播放器使用的代理地址
            # 回源必须携带的请求头（前端拿不到，代理层用得上）
            "requires_headers": bool(fmt.get("http_headers")),
        })
    return sources


def _estimate_size(fmt: dict[str, Any], duration: Any, is_hls: bool) -> int | None:
    """给没有 ``filesize`` 的源估个体积（码率 × 时长 / 8）。

    只在 HLS 且确实缺 filesize 时才算 —— 渐进式 mp4 的 filesize 是站点给的精确值，
    不该被估算值覆盖。拿不到时长或码率就返回 ``None``（前端显示"未知大小"，
    而不是编一个数字）。

    ⚠️ **主清单必须走 :mod:`app.playlist` 实测**，不能用 yt-dlp 的 ``tbr``：
    主清单场景下 ``tbr`` 报的是"最高那档"的码率（实测 6.2 Mbps），而 ffmpeg 实际
    按自身策略选档、产物可能只有 19 MB —— 于是界面显示"下载 MP4（470 MB）"，
    与实际相差 25 倍。这里改用与 :mod:`app.remux` **同一个**分析口径，保证
    「显示的大小」与「点下去真正得到的体积」一致。
    """
    if not is_hls:
        return None
    if fmt.get("filesize") or fmt.get("filesize_approx"):
        return None

    url = fmt.get("url") or ""
    # 主清单（.m3u8 且内容是 #EXT-X-STREAM-INF）→ 交给 playlist 实测
    if _is_manifest_url(url):
        try:
            from .playlist import analyze
            info = analyze(url, follow_master=False)
            est = info.get("estimate_bytes")
            if est:
                return int(est)
            if info.get("duration") and not duration:
                duration = info["duration"]
        except Exception:  # noqa: BLE001 —— 预判失败就退回下面的码率估算
            pass

    try:
        secs = float(duration)
    except (TypeError, ValueError):
        return None
    if not secs or secs <= 0:
        return None
    # tbr 是总码率；没有就 vbr+abr 相加
    kbps = fmt.get("tbr")
    if not kbps:
        parts = [fmt.get("vbr"), fmt.get("abr")]
        kbps = sum(float(p) for p in parts if isinstance(p, (int, float)) and p) or None
    if not kbps:
        return None
    return int(float(kbps) * 1000 * secs / 8)


def _pick_thumbnail(info: dict[str, Any]) -> str | None:
    """挑一张真正的视频封面：过滤站点水印图/UP 主头像/推广图。"""
    thumbs = info.get("thumbnails") or []
    scored: list[tuple[int, str]] = []
    for th in thumbs:
        if not isinstance(th, dict):
            continue
        url = th.get("url") or ""
        if not url.startswith("http"):
            continue
        tag = f"{th.get('id','')} {url}".lower()
        if re.search(r"(?:avatar|face|user|logo|watermark|promo|cover_?ad|favicon)", tag):
            continue
        w, h = th.get("width") or 0, th.get("height") or 0
        scored.append((int(w) * int(h), url))
    if scored:
        return max(scored, key=lambda t: t[0])[1]
    return info.get("thumbnail")


def _title_of(entry: dict[str, Any], url: str, sources: list[dict[str, Any]]) -> str:
    """投影标题，并对「裸 m3u8 直链」做诚实兜底。

    痛点：粘贴的是一条 ``https://cdn.xx.com/xxx/656e70….m3u8``（甚至不带扩展名）的
    直链时，yt-dlp 只能落到通用提取器，把 **URL 文件名当标题** —— 那串哈希毫无意义，
    界面上就显示成乱码一样的"标题"（用户反馈的两个 bug 之一）。

    判定（与站点无关，不依赖任何页面提取器 —— 这类站的页面地址 yt-dlp 普遍不支持）：
      1. 标题 ≠ URL 文件名 → 说明有真实元信息，正常返回；
      2. 链接路径是清单（.m3u8/.mpd），或**所有可播源都是 HLS**（CDN 常给无扩展名的
         m3u8 地址）→ 断言"直链不含标题信息"，用中文说明顶替哈希；
      3. 其余情况保留原值（如 mov_bbb.mp4 → "mov_bbb"，文件名本身就是可用的名字）。
    """
    raw = (entry.get("title") or "").strip()
    stem = _url_stem(url)
    if raw and (not stem or raw.lower() != stem.lower()):
        return raw[:200]                      # 标题来自真实元信息
    if _is_manifest_url(url) or (sources and all(s.get("is_hls") for s in sources)):
        return "未命名视频（m3u8 直链不含标题信息）"
    return (raw or "未命名视频")[:200]


def _try_scrape(url: str, notify: Callable[[str], None]) -> dict[str, Any] | None:
    """yt-dlp 不认这个站点时，退一步读页面 HTML 取标题与直链。

    返回 ``{"title", "media"}``；页面读不到、或既没有标题也没有直链时返回 ``None``
    （调用方照旧抛原本的"不支持"错误，行为与从前一致）。
    """
    from . import scraper                # 延迟导入：只在真正需要兜底时才加载

    notify("该站点无解析器，尝试直接读取页面…")
    try:
        got = scraper.scrape(url)
    except Exception as exc:             # noqa: BLE001 —— 兜底路径绝不让主流程崩
        log.debug("scrape fallback crashed: %s", exc)
        return None
    if not got.get("title") and not got.get("media"):
        return None
    return got


def _scraped_entry(scraped: dict[str, Any]) -> dict[str, Any]:
    """把兜底结果包装成 yt-dlp 风格的 entry，好复用同一条投影链路。"""
    return {"title": scraped.get("title") or ""}


def _scraped_formats(scraped: dict[str, Any], url: str) -> list[dict[str, Any]]:
    """把兜底扫到的直链转成 ``_pick_sources`` 认识的 format 字典。

    只给必要字段：``url`` / ``protocol`` / ``ext`` / ``http_headers``。
    清晰度、码率等元信息页面里拿不到，就让它们为空 —— 前端已有"无标签"的兜底渲染，
    宁可少显示字段，也不编造一个假的分辨率。

    ``http_headers`` 带上页面地址作 ``Referer``：聚合站的 CDN 常校验防盗链，
    没有 Referer 会 403（真机上就是这么被拦的）。

    ``_group`` 标记这是页面里的**第几个视频**：一个页面可能挂着多个播放器
    （各自独立的视频），它们的清晰度/编码元信息全为空，若不分组就会在
    `_pick_sources` 的去重环节被折叠成一个，用户只能拿到其中一个视频。
    """
    formats: list[dict[str, Any]] = []
    referer = {"Referer": url, "User-Agent": _UA}
    for group, media_url in enumerate(scraped.get("media") or []):
        is_hls = _is_manifest_url(media_url) or ".m3u8" in media_url.lower()
        ext = "mp4"
        if not is_hls:
            if re.search(r"\.webm(\?|$)", media_url, re.I):
                ext = "webm"
            elif re.search(r"\.flv(\?|$)", media_url, re.I):
                ext = "flv"
        formats.append({
            "url": media_url,
            "protocol": "m3u8_native" if is_hls else "https",
            "ext": ext,
            "http_headers": dict(referer),
            "_group": group,              # 第几个视频（见上）
        })
    return formats


def _is_manifest_url(url: str) -> bool:
    """路径（不含查询串）是否为清单文件。"""
    path = re.sub(r"[?#].*$", "", url or "").lower()
    return path.endswith(".m3u8") or path.endswith(".mpd")


def _url_stem(url: str) -> str:
    """取 URL 路径最后一段的文件名主干（无扩展名），失败返回空串。"""
    tail = re.sub(r"[?#].*$", "", url or "").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0].strip()[:200]


def extract(url: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """解析分享链接，返回精简结果字典。阻塞调用，请在 ``asyncio.to_thread`` 里跑。"""
    from yt_dlp import YoutubeDL  # 延迟导入：让 `--help`/单测不必等 yt-dlp 冷启动

    notify = progress or (lambda _s: None)
    notify("连接站点，识别视频信息…")

    opts = _build_ydl_opts()
    logger = _YdlLogger()
    opts["logger"] = logger
    scraped: dict[str, Any] | None = None
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 —— yt-dlp 抛的是 DownloadError 等杂类
        code, friendly = classify_error(str(exc))
        log.debug("yt-dlp raised: %s", exc)
        # yt-dlp 没有对应提取器 ≠ 取不到：这类站的播放地址常明文写在页面里。
        # 兜底读一次页面（只读、限体积、best-effort），拿到标题/直链就继续走正常投影。
        if code == "unsupported" and config.SCRAPE_FALLBACK:
            scraped = _try_scrape(url, notify)
            if scraped is None:
                raise ExtractError(friendly, code) from exc
            info = None
        else:
            raise ExtractError(friendly, code) from exc

    if info is None and scraped is None:
        # ignoreerrors=True 时异常被吞掉，只能从 logger 里把根因捞回来
        code, friendly = classify_error(logger.latest())
        # 同上：哪怕是"被吞掉"的 Unsupported URL，也给页面兜底一次机会
        if code == "unsupported" and config.SCRAPE_FALLBACK:
            scraped = _try_scrape(url, notify)
        if scraped is None:
            raise ExtractError(friendly, code)

    notify("过滤广告与冗余字段，整理可播放源…")

    if scraped is not None:
        entry, formats = _scraped_entry(scraped), _scraped_formats(scraped, url)
    else:
        entry = _first_entry(info)
        formats = entry.get("formats")
        if not isinstance(formats, list):
            # 有些提取器只给单个 url（如部分短视频）
            formats = [entry] if entry.get("url") else []

    # 时长要在这里取出：_pick_sources 用它给 HLS 源估算体积（码率 × 时长）
    duration = entry.get("duration")
    sources = _pick_sources(formats, duration)
    if not sources:
        raise ExtractError("该视频无可直接播放的源（可能被加密、仅音频，或需付费登录）", "no_source")

    provider, color = identify(url)

    # —— 白名单投影：UI 之外的字段到此为止，绝不外泄 ——
    return {
        "title": _title_of(entry, url, sources),
        "provider": provider,
        "provider_color": color,
        "duration": int(duration) if duration else None,
        "thumbnail": _pick_thumbnail(entry),
        "is_live": bool(entry.get("is_live")),
        "sources": sources,
        # 默认选中：优先「音画齐全的最高清晰度」，否则退到最低码率的 HLS（保证一定能播）
        "default_index": _default_index(sources),
        "filtered": {
            "removed_ads": True,     # 前端据此显示「已过滤广告」徽标
            "max_sources": _MAX_SOURCES,
        },
    }


def _default_index(sources: list[dict[str, Any]]) -> int:
    """选定默认播放源下标：可原生播放 → 取最高的 muxed mp4；否则取最稳的 HLS。

    ``hls[-1]`` 的原意是"同一个视频的多个 HLS 清晰度里挑码率最低的那档，保证能播"，
    因为源列表按清晰度降序排列。但页面兜底解析出的多个 HLS 是**并列的不同视频**
    （没有清晰度信息、unsorted），此时取最后一个等于默认去播"第二个视频"，
    用户会以为第一个视频丢了。故仅在"确实像清晰度分档"时才取末尾。
    """
    native = [i for i, s in enumerate(sources) if not s["is_hls"]]
    if native:
        return native[0] if len(native) == 1 else next(
            (i for i in native if sources[i]["height"] >= 720), native[0]
        )
    hls = [i for i, s in enumerate(sources) if s["is_hls"]]
    if not hls:
        return 0
    # 有清晰度信息才认为是"同一视频的分档"，否则当作并列视频、默认播第一个
    has_quality_info = any(sources[i].get("height") for i in hls)
    return hls[-1] if has_quality_info else hls[0]


# ---- 便于直接跑单文件调试：python -m app.extractor <url> ----
if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    from .security import check_url

    print(json.dumps(extract(check_url(sys.argv[1])), ensure_ascii=False, indent=2))
