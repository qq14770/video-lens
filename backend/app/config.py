"""运行期配置。

所有配置项均可用环境变量覆盖（前缀 ``VX_``），默认值面向「本机开发」场景。
生产部署务必显式设置 ``VX_SECRET``（令牌签名密钥）并按需收紧 ``VX_MAX_*``。
"""

from __future__ import annotations

import os
import secrets


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认（避免配置笔误直接让服务起不来）。"""
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class Config:
    """进程级单例配置（导入时求值一次）。"""

    # ---- 网络监听 ----
    HOST: str = os.getenv("VX_HOST", "127.0.0.1")   # 默认只绑回环；对外服务请改 0.0.0.0
    PORT: int = _env_int("VX_PORT", 8300)

    # ---- 安全 ----
    # 令牌密钥：未设置则每次启动随机生成（重启后旧代理链接失效，属预期行为）
    SECRET: bytes = os.getenv("VX_SECRET", "").encode() or secrets.token_bytes(32)
    #: 密钥是否来自环境变量（False = 本次随机生成）
    SECRET_FROM_ENV: bool = bool(os.getenv("VX_SECRET", "").strip())
    #: 密钥是否还是模板里的占位值 —— 占位值等于是**公开密钥**，
    #: 任何人都能伪造合法的播放/下载令牌，必须显式告警（见 main 的启动自检）。
    SECRET_IS_PLACEHOLDER: bool = any(
        marker in os.getenv("VX_SECRET", "")
        for marker in ("请替换", "CHANGE_ME", "changeme", "your-secret", "REPLACE_ME")
    )
    TOKEN_TTL: int = _env_int("VX_TOKEN_TTL", 6 * 3600)          # 代理链接有效期（秒）
    MAX_URL_LEN: int = _env_int("VX_MAX_URL_LEN", 2048)          # 允许输入的分享链接最大长度
    RATE_LIMIT_RPM: int = _env_int("VX_RATE_LIMIT_RPM", 30)      # 每 IP 每分钟提取次数
    BLOCK_PRIVATE_NET: bool = _env_bool("VX_BLOCK_PRIVATE_NET", True)  # SSRF：禁止解析到内网

    # ---- 解析行为 ----
    EXTRACT_TIMEOUT: int = _env_int("VX_EXTRACT_TIMEOUT", 45)    # 单任务最长解析时间（秒）
    PROXY_TIMEOUT: int = _env_int("VX_PROXY_TIMEOUT", 30)        # 回源首包超时（秒）
    PROXY_CONCURRENCY: int = _env_int("VX_PROXY_CONCURRENCY", 16)  # 同时回源的代理请求数
    MAX_MANIFEST_BYTES: int = _env_int("VX_MAX_MANIFEST_BYTES", 4 * 1024 * 1024)  # m3u8 改写体积上限
    # 可选：yt-dlp 的 curl_cffi 浏览器指纹（需安装 curl_cffi，如 "chrome-124"）。留空则不启用。
    IMPERSONATE: str = os.getenv("VX_IMPERSONATE", "").strip()

    # ---- 页面兜底解析（yt-dlp 无提取器的站点）----
    # yt-dlp 抛 Unsupported URL 时，退一步直接读页面 HTML 取标题与播放直链。
    SCRAPE_FALLBACK: bool = _env_bool("VX_SCRAPE_FALLBACK", True)  # 关掉即恢复"不支持就报错"
    SCRAPE_MAX_BYTES: int = _env_int(                             # 页面读取体积上限
        "VX_SCRAPE_MAX_BYTES", 2 * 1024 * 1024)

    # ---- 任务表 ----
    JOB_TTL: int = _env_int("VX_JOB_TTL", 3600)                  # 已完成任务的内存保留时长
    JOB_CAPACITY: int = _env_int("VX_JOB_CAPACITY", 256)         # 内存任务表容量（LRU 淘汰）

    # ---- HLS 合并下载（服务端 ffmpeg remux）----
    # m3u8 是分片清单而非视频文件，直接下载会得到打不开的文本；
    # 本组配置控制「服务端合并为 MP4」的行为。找不到 ffmpeg 时功能自动降级为原样下载清单。
    FFMPEG_PATH: str = os.getenv("VX_FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"
    REMUX_TIMEOUT: int = _env_int("VX_REMUX_TIMEOUT", 900)       # 单次合并最长秒数（≈下载一遍）
    REMUX_CONCURRENCY: int = _env_int("VX_REMUX_CONCURRENCY", 2) # 同时进行的合并任务数
    REMUX_MAX_BYTES: int = _env_int(                             # 产物体积上限，超限即中止
        "VX_REMUX_MAX_BYTES", 2 * 1024 * 1024 * 1024)
    REMUX_TEMP_DIR: str = os.getenv("VX_REMUX_TEMP_DIR", "").strip() or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".remux_tmp")

    # ---- 批量下载 ----
    # 一次提交多条链接，逐条提取（HLS 再合并），最后打包成一个 zip 取走。
    BATCH_MAX_URLS: int = _env_int("VX_BATCH_MAX_URLS", 20)          # 单批最多链接数
    BATCH_CONCURRENCY: int = _env_int("VX_BATCH_CONCURRENCY", 2)     # 同时处理的条目数
    BATCH_TIMEOUT: int = _env_int("VX_BATCH_TIMEOUT", 3600)          # 整批看门狗（秒）
    BATCH_ZIP_MAX_BYTES: int = _env_int(                             # 打包产物体积上限
        "VX_BATCH_ZIP_MAX_BYTES", 8 * 1024 * 1024 * 1024)

    # ---- 定时任务 ----
    SCHEDULE_ENABLED: bool = _env_bool("VX_SCHEDULE_ENABLED", True)  # 关掉即停用定时调度
    SCHEDULE_TICK: int = _env_int("VX_SCHEDULE_TICK", 20)            # 调度器轮询间隔（秒）
    SCHEDULE_MISSED_GRACE: int = _env_int(                           # 错过多久内仍补跑（秒）
        "VX_SCHEDULE_MISSED_GRACE", 3600)
    STORE_FILE: str = os.getenv("VX_STORE_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.json")

    # ---- 静态资源（前端）----
    # 目录约定：video-extract/
    #             ├── backend/app/config.py   ← 本文件（上溯两级 = backend/，三级 = 项目根）
    #             └── frontend/
    STATIC_DIR: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend"
    )


config = Config()
