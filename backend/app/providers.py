"""站点识别：把主机名映射成友好的来源标签，仅用于界面展示。

不用于安全判定（安全边界在 security.py），因此可以只维护常见站点，
未知站点统一回退为「通用」，不影响解析能力（由 yt-dlp 的提取器决定）。
"""

from __future__ import annotations

from urllib.parse import urlparse

# host 后缀 → (显示名, 品牌色)
_PROVIDERS: list[tuple[str, str, str]] = [
    ("youtube.com",  "YouTube",  "#ff4e42"),
    ("youtu.be",     "YouTube",  "#ff4e42"),
    ("bilibili.com", "哔哩哔哩", "#fb7299"),
    ("b23.tv",       "哔哩哔哩", "#fb7299"),
    ("douyin.com",   "抖音",     "#111214"),
    ("iesdouyin.com","抖音",     "#111214"),
    ("v.douyin.com", "抖音",     "#111214"),
    ("xiaohongshu.com","小红书", "#ff2442"),
    ("xhslink.com",  "小红书",   "#ff2442"),
    ("weibo.com",    "微博",     "#e6162d"),
    ("v.weibo.com",  "微博视频", "#e6162d"),
    ("twitter.com",  "X / Twitter", "#1d9bf0"),
    ("x.com",        "X / Twitter", "#1d9bf0"),
    ("t.co",         "X / Twitter", "#1d9bf0"),
    ("vimeo.com",    "Vimeo",    "#53b7ff"),
    ("dailymotion.com", "Dailymotion", "#0b86ff"),
    ("tencent.com",  "腾讯视频", "#ff9f1a"),
    ("v.qq.com",     "腾讯视频", "#ff9f1a"),
    ("youku.com",    "优酷",     "#1d8dff"),
    ("iqiyi.com",    "爱奇艺",   "#00be06"),
    ("mgtv.com",     "芒果TV",   "#ff5900"),
    ("acfun.cn",     "AcFun",    "#fd4c5d"),
    ("tiktok.com",   "TikTok",   "#111214"),
]


def identify(url: str) -> tuple[str, str]:
    """返回 ``(来源名, 品牌色)``。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "通用", "#5b6472"
    for suffix, name, color in _PROVIDERS:
        if host == suffix or host.endswith("." + suffix):
            return name, color
    if not host:
        return "通用", "#5b6472"
    # 未收录站点：取「主域名」而非第一段（www.cdn.example.com → Example）。
    # 直接把 "www" 展示给用户是没有信息量的噪音。
    labels = [p for p in host.split(".") if p and p not in ("www", "m", "i", "v", "api", "cdn", "static")]
    base = labels[-2] if len(labels) >= 2 else (labels[0] if labels else host)
    return base.capitalize(), "#5b6472"
