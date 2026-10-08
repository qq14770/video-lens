"""合并前的轻量预判：估算产物大小与进度分母。

**要解决的问题**：用户在点「下载 MP4」之前不知道要下多少、点下去之后也不知道还要等多久。
两件事都缺一个"分母"：

  * 体积 → 需要「码率 × 时长」
  * 进度 → 需要「总时长」（或总分片数）

**能拿到什么**（都来自清单本身，不需要预下载分片）：

  * ``#EXT-X-STREAM-INF:...BANDWIDTH=2149280`` —— 变体码率（bps）
    站点自己声明的，实测与产物吻合度很高，优先用它；
  * ``#EXTINF:10.000,`` 逐行累加 —— 精确总时长；
  * 分片 ``Content-Length`` —— 最准，但要逐个 HEAD。只在没有 BANDWIDTH 时
    抽样几个分片外推（实测误差 3.6%）。

**估算公式**：``大小 ≈ 码率(bps) × 时长(s) / 8``。转封装不重编码，所以这个估算
几乎就是产物大小（容器开销可忽略）。

**为什么单独一个模块**：它既被 ``remux`` 用来算进度，也被 ``extractor`` 用来在
提取结果里带上预估大小，放任何一边都会形成循环依赖。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import config
from .stream_proxy import headers_jar

log = logging.getLogger("video-lens.playlist")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)", re.I)
_AVG_BANDWIDTH_RE = re.compile(r"AVERAGE-BANDWIDTH=(\d+)", re.I)
_EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)", re.I)


def _headers_for(url: str) -> dict[str, str]:
    """回源清单要带提取时暂存的头（防盗链站点否则 403）。"""
    jar = dict(headers_jar.get(url))
    jar.setdefault("User-Agent", _UA)
    return jar


def is_manifest(url: str) -> bool:
    path = re.sub(r"[?#].*$", "", url or "").lower()
    return path.endswith(".m3u8") or path.endswith(".mpd")


def analyze(url: str, *, follow_master: bool = True,
            sample_segments: bool = True) -> dict[str, Any]:
    """分析一个 HLS 清单，返回可用的预判信息。

    返回 ``{"duration": float|None, "bandwidth": int|None, "segments": int,
    "estimate_bytes": int|None, "is_master": bool, "resolved": str}``。

    ``follow_master``：遇到主清单时要不要往下钻一层取变体。

    ⚠️ **本项目的调用方几乎都应该传 ``False``**。因为 ``remux`` 拿到的
    ``direct_url`` 是 yt-dlp 已经解析好的**具体某一档**地址（用户在前端选的清晰度
    就体现在这里），此刻再去主清单里"挑一个变体"等于把用户的选择丢掉：
    实测主清单最高变体 6.2 Mbps（471 MB），而 ffmpeg 实际读的是 246 kbps 那档
    （19.1 MB），估算会差 25 倍。

    只有"手上只有一个主清单、必须猜 ffmpeg 会选哪档"时才用 ``True``。

    全程 best-effort：任何一步失败都只是少一个字段，绝不抛异常影响下载主流程。
    """
    out: dict[str, Any] = {
        "duration": None, "bandwidth": None, "segments": 0,
        "estimate_bytes": None, "is_master": False, "resolved": url,
    }
    text = _fetch(url)
    if not text:
        return out

    try:
        variants = _parse_variants(text, url)
        if variants:
            out["is_master"] = True
            # 主清单自身没有分片，码率取其中位档作为保守估计（见 docstring）
            if follow_master:
                best = max(variants, key=lambda v: v["bandwidth"])
                out["bandwidth"] = best["bandwidth"]
                out["resolved"] = best["url"]
                sub = _fetch(best["url"])
                if sub:
                    text = sub
            else:
                bw = sorted(v["bandwidth"] for v in variants)
                out["bandwidth"] = bw[len(bw) // 2] if bw else None

        duration, segments = _parse_media(text)
        if duration:
            out["duration"] = duration
        out["segments"] = segments

        # 码率：清单里的 BANDWIDTH 优先；没有就抽样分片外推
        if not out["bandwidth"]:
            out["bandwidth"] = _bandwidth_from_media(text)
        if not out["bandwidth"] and sample_segments and segments:
            out["bandwidth"] = _bandwidth_from_samples(out["resolved"], text)

        if out["bandwidth"] and out["duration"]:
            out["estimate_bytes"] = int(out["bandwidth"] * out["duration"] / 8)
    except Exception as exc:  # noqa: BLE001 —— 预判失败不影响下载
        log.debug("analyze failed for %s: %s", url, exc)
    return out


def _fetch(url: str, limit: int = 512 * 1024) -> str:
    try:
        with httpx.Client(timeout=min(config.EXTRACT_TIMEOUT, 15), follow_redirects=True,
                          headers=_headers_for(url), verify=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text[:limit]
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch manifest failed %s: %s", url, exc)
        return ""


def _parse_variants(text: str, base: str) -> list[dict[str, Any]]:
    """解析主清单里的 ``#EXT-X-STREAM-INF`` → 变体列表（码率 + 绝对地址）。"""
    if "#EXT-X-STREAM-INF" not in text:
        return []
    out: list[dict[str, Any]] = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        # AVERAGE-BANDWIDTH 更贴近实际平均码率，优先
        m = _AVG_BANDWIDTH_RE.search(line) or _BANDWIDTH_RE.search(line)
        bw = int(m.group(1)) if m else 0
        # 变体地址在紧随其后的第一个非注释行
        for nxt in lines[i + 1:]:
            if nxt and not nxt.startswith("#"):
                out.append({"bandwidth": bw, "url": urljoin(base, nxt)})
                break
    return out


def _parse_media(text: str) -> tuple[float | None, int]:
    """媒体清单：累加 ``#EXTINF`` 得总时长，顺便数分片。"""
    duration = 0.0
    count = 0
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        m = _EXTINF_RE.match(line.strip())
        if m:
            try:
                duration += float(m.group(1))
                count += 1
            except ValueError:
                pass
    # 直播清单没有总时长（没有 #EXT-X-ENDLIST）→ 明确返回 None，不要给个假分母
    if "#EXT-X-ENDLIST" not in text and duration:
        return None, count
    return (duration or None), count


def _bandwidth_from_media(text: str) -> int | None:
    """媒体清单偶尔自带 ``#EXT-X-BITRATE``（单位 kbps）。"""
    m = re.search(r"#EXT-X-BITRATE:\s*(\d+)", text, re.I)
    return int(m.group(1)) * 1000 if m else None


def _bandwidth_from_samples(media_url: str, text: str, sample: int = 3) -> int | None:
    """没有声明码率时：抽样前几个分片的 Content-Length，按时长外推。

    实测误差约 3.6%，作为"预计大小"完全够用；比逐个 HEAD 整个清单快得多。
    """
    segs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not segs:
        return None
    durs = [float(m.group(1)) for m in
            (_EXTINF_RE.match(ln.strip()) for ln in text.splitlines() if ln.startswith("#EXTINF"))
            if m]
    picked = list(zip(segs, durs))[:sample]
    if not picked:
        return None
    total_bytes = 0
    total_secs = 0.0
    try:
        with httpx.Client(timeout=10, follow_redirects=True,
                          headers=_headers_for(media_url), verify=False) as client:
            for seg, dur in picked:
                resp = client.head(urljoin(media_url, seg))
                size = int(resp.headers.get("content-length") or 0)
                if size > 0:
                    total_bytes += size
                    total_secs += dur
    except Exception:  # noqa: BLE001
        return None
    if total_bytes <= 0 or total_secs <= 0:
        return None
    return int(total_bytes * 8 / total_secs)


def describe_size(n: int | None) -> str:
    """人读体积文案（供后端直接塞进结果，前端不必重复实现）。"""
    if not n or n <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(n)
    idx = 0
    while val >= 1024 and idx < len(units) - 1:
        val /= 1024
        idx += 1
    return f"{val:.0f} {units[idx]}" if val >= 100 or idx == 0 else f"{val:.1f} {units[idx]}"
