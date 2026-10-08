"""安全相关：URL 合法性校验（SSRF 防护）+ 代理链接 HMAC 令牌。

设计要点：
  1. 只接受 http/https，禁止 userinfo（防 ``http://host@evil/`` 式混淆）。
  2. 主机名解析后必须为公网地址（可关闭，见 ``config.BLOCK_PRIVATE_NET``）。
  3. 代理链接的令牌对「原始直链 + 过期时间」签名，防止服务被当作任意 URL 转发器（open proxy）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import socket
import time
from urllib.parse import urlparse

from .config import config

# 分享链接允许的最大长度已在 config.MAX_URL_LEN；这里再做一次字符集粗筛，挡掉明显的注入尝试
_UNSAFE_CHARS = re.compile(r"[\r\n\t\x00-\x1f\x7f]")


class UrlError(ValueError):
    """用户输入的链接不合法（映射为 HTTP 400）。"""


def _sign(payload: str) -> str:
    """对任意字符串做 HMAC-SHA256，返回 urlsafe 且去掉填充符的短签名。"""
    digest = hmac.new(config.SECRET, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_token(url: str, ttl: int | None = None) -> str:
    """签发代理访问令牌：``<过期时间戳>.<签名>``。

    签名覆盖 ``exp`` 与规范化后的 URL，因此令牌既不能续期、也不能改指到别的直链。
    """
    exp = int(time.time()) + (ttl if ttl is not None else config.TOKEN_TTL)
    return f"{exp}.{_sign(f'{exp}|{url}')}"


def verify_token(url: str, token: str | None) -> bool:
    """校验令牌（常量时间比较 + 过期检查）。"""
    if not token or "." not in token:
        return False
    exp_str, sig = token.split(".", 1)
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(f"{exp}|{url}"))


def normalize(url: str) -> str:
    """去除首尾空白并补全缺失的 scheme（用户常在微信里粘贴 ``b23.tv/xxx``）。"""
    url = (url or "").strip()
    if not url:
        raise UrlError("链接为空")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        # 只有形如 host/path 的裸链接才补 https
        if re.match(r"^[A-Za-z0-9.-]+(:\d+)?(/[^\s]*)?$", url):
            url = "https://" + url
        else:
            raise UrlError("无法识别的链接格式")
    return url


def _host_ips(host: str) -> list[str]:
    """解析主机的全部 A/AAAA 记录（失败时返回空表，交由调用方判定）。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return sorted({info[4][0] for info in infos})


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # 明确排除：私有段、回环、链路本地、保留段、组播
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def check_url(url: str) -> str:
    """校验用户提交的分享链接，返回规范化后的 URL；不合法抛 :class:`UrlError`。

    注意：这里**不**限制站点域名，因为 yt-dlp 支持上千个提取器；域名白名单会误杀
    「短链 + 302 跳转」的场景（如 b23.tv → bilibili）。SSRF 的防线放在解析后的 IP 上。
    """
    url = normalize(url)
    if len(url) > config.MAX_URL_LEN:
        raise UrlError(f"链接过长（超过 {config.MAX_URL_LEN} 字符）")
    if _UNSAFE_CHARS.search(url):
        raise UrlError("链接包含非法控制字符")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlError("仅支持 http/https 链接")
    if parsed.username or parsed.password:
        raise UrlError("链接不允许携带账号密码")
    if not parsed.hostname:
        raise UrlError("链接缺少主机名")

    host = parsed.hostname
    # 字面量 IP 直接判定；域名则解析后逐个判定（防 DNS 指向 127.0.0.1 / 169.254.169.254）
    if config.BLOCK_PRIVATE_NET:
        if _is_ip_literal(host):
            if not _ip_is_public(host):
                raise UrlError("不允许访问内网地址")
        else:
            ips = _host_ips(host)
            if not ips:
                raise UrlError("无法解析该域名")
            if not any(_ip_is_public(ip) for ip in ips):
                raise UrlError("不允许访问内网地址")
    return url


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_public_url(url: str) -> bool:
    """供 stream_proxy 复用：直链回源前同样要做一次 IP 校验。"""
    try:
        check_url(url)
        return True
    except UrlError:
        return False
