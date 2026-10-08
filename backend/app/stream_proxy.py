"""直链反向代理 —— 让「复制到的直链」在本页播放器里真正能播。

要解决的四个现实问题：

  1. **CORS**：站点 CDN 通常不下发 ``Access-Control-Allow-Origin``，前端 <video> 直连必失败。
  2. **防盗链**：B 站/抖音/小红书校验 ``Referer`` + ``User-Agent``，浏览器 JS 无权设置这两个头。
  3. **混合内容**：直链常是 ``http://``，在 https 页面里会被浏览器拦截。
  4. **HLS 跨域**：m3u8 本身能取到，但里面的分片 URI 是绝对地址，同样会被 CORS 拦。

对策：所有播放请求走本机 ``/api/stream``，由服务端带上前端拿不到的请求头回源；
m3u8 文本按行改写，把分片地址也拉到本机；``Range`` 头透传以支持进度条拖动。

安全：代理必须持 HMAC 令牌（:func:`app.security.make_token`），否则本服务会退化成
任意 URL 转发器（open proxy）。令牌绑定「原始直链 + 过期时间」，不可改指、不可续期。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from collections import OrderedDict
from typing import AsyncIterator
from urllib.parse import urljoin

import httpx

from .config import config
from .security import check_url, make_token, verify_token

log = logging.getLogger("video-lens.proxy")

# 需要按文本改写的 HLS 清单（Content-Type 五花八门，命中关键词即可）
_MANIFEST_CT = re.compile(r"(?:mpegurl|m3u8)", re.I)

# ---------------------------------------------------------------------------
# 请求头暂存：提取时把 yt-dlp 拿到的 http_headers 存下来，代理回源时原样附上
# ---------------------------------------------------------------------------


class HeaderJar:
    """``sha1(直链) → 请求头`` 的小容量 LRU。

    为什么不把 header 编进 URL 参数：Cookie/UA 属于敏感信息，放 URL 会进浏览器历史、
    会进访问日志；放服务端内存只随令牌生命周期存在，泄露面小得多。
    """

    def __init__(self, capacity: int = 512) -> None:
        self._store: "OrderedDict[str, dict[str, str]]" = OrderedDict()
        self._capacity = capacity

    def put(self, url: str, headers: dict[str, str] | None) -> str:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        clean = {k: v for k, v in (headers or {}).items()
                 if k.lower() not in ("host", "connection", "accept-encoding")}
        self._store[key] = clean
        self._store.move_to_end(key)
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)
        return key

    def get(self, url: str) -> dict[str, str]:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        headers = self._store.get(key)
        if headers is None:
            # 令牌有效但内存里已淘汰（例如服务重启后继续点播放）→ 退回通用 UA，让站点自己判定
            return {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        return headers


headers_jar = HeaderJar()
_semaphore: asyncio.Semaphore | None = None


def _limit() -> asyncio.Semaphore:
    """惰性创建信号量（必须在事件循环内创建，避免跨 loop 绑定问题）。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.PROXY_CONCURRENCY)
    return _semaphore


# ---------------------------------------------------------------------------
# 链接签发 / 解析
# ---------------------------------------------------------------------------

def play_url(raw_url: str, req_headers: dict[str, str] | None = None) -> str:
    """生成可播放的本机代理地址（前端只看到这个以 ``/`` 开头的相对路径）。"""
    headers_jar.put(raw_url, req_headers)
    return f"/api/stream?u={_enc(raw_url)}&t={make_token(raw_url)}"


def manifest_url(raw_url: str, parent: str) -> str:
    """清单内部的资源地址 → 本机代理地址（沿用父清单的防盗链头）。"""
    headers_jar.put(raw_url, headers_jar.get(parent))
    return play_url(raw_url)


def _enc(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _dec(token_blob: str) -> str:
    """还原 base64url（补齐填充符）。"""
    pad = "=" * (-len(token_blob) % 4)
    return base64.urlsafe_b64decode(token_blob + pad).decode("utf-8")


class ProxyError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def open_target(u: str, t: str | None) -> str:
    """校验参数并返回原始直链；任何不合法组合抛 :class:`ProxyError`。"""
    try:
        raw = _dec(u)
    except Exception as exc:  # noqa: BLE001
        raise ProxyError("代理参数无法解码", 400) from exc
    if not verify_token(raw, t):
        raise ProxyError("代理链接无效或已过期，请重新提取", 403)
    try:
        return check_url(raw)                    # 直链同样要过 SSRF 检查
    except ValueError as exc:
        raise ProxyError(str(exc), 400) from exc


# ---------------------------------------------------------------------------
# 回源
# ---------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(config.PROXY_TIMEOUT, read=None),  # 读不设超时：大文件要能一直流
        limits=httpx.Limits(max_connections=config.PROXY_CONCURRENCY * 2,
                            max_keepalive_connections=config.PROXY_CONCURRENCY),
    )


async def head_info(url: str, headers: dict[str, str]) -> httpx.Response | None:
    """尽力拿到 Content-Type（部分站点 HEAD 不通则返回 None）。"""
    try:
        async with _client() as client:
            return await client.head(url, headers=headers)
    except httpx.HTTPError:
        return None


async def stream(url: str, byte_range: str | None) -> tuple[httpx.Response, AsyncIterator[bytes]]:
    """打开一条到 CDN 的流式响应，交由调用方（StreamingResponse）边收边发。"""
    req_headers = dict(headers_jar.get(url))
    if byte_range:
        req_headers["Range"] = byte_range
    client = _client()
    request = client.build_request("GET", url, headers=req_headers)
    async with _limit():
        try:
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise ProxyError(f"回源失败：{exc.__class__.__name__}", 502) from exc

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise ProxyError(f"源站返回 {resp.status_code}（直链可能已过期，请重新提取）",
                         502 if resp.status_code in (403, 404, 410, 412) else 502)

    async def body() -> AsyncIterator[bytes]:
        """原样转发字节（``aiter_raw`` 不做解压，因此 ``Content-Length`` /
        ``Content-Encoding`` / ``Content-Range`` 与源站保持一致，播放器 seek 才准确），
        并在结束/异常时彻底关闭底层连接。"""
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return resp, body()


_PASSTHROUGH = ("content-type", "content-length", "content-range", "accept-ranges",
                "cache-control", "etag", "last-modified", "content-encoding",
                "content-disposition")


def response_headers(resp: httpx.Response) -> dict[str, str]:
    """只透传播放器关心的头，其余（Set-Cookie、CDN 私有头）一律剥掉。"""
    out = {k: v for k, v in resp.headers.items() if k.lower() in _PASSTHROUGH}
    out.setdefault("Accept-Ranges", "bytes")
    out.setdefault("Cache-Control", "private, max-age=60")
    out.setdefault("Content-Disposition", "inline")   # 强制内联播放而非下载
    out["X-Content-Type-Options"] = "nosniff"
    return out


# ---------------------------------------------------------------------------
# HLS 清单改写
# ---------------------------------------------------------------------------

_COMMENT_OR_TAG = re.compile(r"^\s*#")


async def fetch_manifest(url: str) -> str:
    """一次性取回 m3u8 文本（改写需要完整内容，不能流式）。"""
    headers = dict(headers_jar.get(url))
    headers.pop("Range", None)
    async with _client() as client:
        async with _limit():
            resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise ProxyError(f"清单拉取失败：源站 {resp.status_code}", 502)
    if len(resp.content) > config.MAX_MANIFEST_BYTES:
        raise ProxyError("清单体积过大，已拒绝改写（请直接用直链播放）", 507)
    charset = resp.encoding or "utf-8"
    return resp.content.decode(charset, errors="replace")


def rewrite_manifest(text: str, base_url: str) -> str:
    """把 m3u8 里的相对/绝对资源地址全部指向本机代理。

    规则：
      - ``#`` 开头的标签行原样保留（EXTINF/EXT-X-* 等，播放器解析依赖它们）；
      - ``EXT-X-KEY:URI="..."`` / ``EXT-X-MAP:URI="..."`` 的 URI 就地换成代理地址；
      - 非注释行即资源地址（分片或子清单），拼接绝对 URL 后换代理地址。
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if _COMMENT_OR_TAG.match(stripped):
            out.append(_rewrite_tag_uri(stripped, base_url))
            continue
        out.append(_proxy_for(_abs(stripped, base_url), base_url))
    # 结尾补 #EXT-X-ENDLIST 之外的原样性：保持行尾 LF
    return "\n".join(out) + "\n"


def _rewrite_tag_uri(line: str, base_url: str) -> str:
    """改写标签内 ``URI="..."`` 的取值（只处理第一个 URI= 属性，够覆盖 KEY/MAP）。"""
    match = re.search(r'URI="([^"]+)"', line)
    if not match:
        return line
    inner = match.group(1)
    if inner.startswith("data:") or inner.startswith("sk-"):
        return line                                  # 内联密钥/程序化密钥不改
    return line[: match.start(1)] + _proxy_for(_abs(inner, base_url), base_url) + line[match.end(1):]


def _proxy_for(abs_url: str, parent_url: str) -> str:
    """清单内资源 → 以 ``/`` 开头的代理路径。

    以 ``/`` 开头的地址会被播放器按「清单自身的 origin」解析，
    也就是回到本机 ``/api/stream``，因此分片请求同样带上防盗链头。
    """
    return manifest_url(abs_url, parent_url)


def _abs(ref: str, base_url: str) -> str:
    return urljoin(base_url, ref)


def looks_like_manifest(url: str, content_type: str = "") -> bool:
    """按扩展名或 Content-Type 判断是否为 HLS 清单。"""
    path = re.sub(r"[?#].*$", "", url).lower()
    return path.endswith(".m3u8") or bool(_MANIFEST_CT.search(content_type or ""))
