"""HLS 直链的服务端合并下载（remux）。

要解决的痛点：**m3u8 能预览、下载却得到废文件。**
m3u8 只是「分片清单」，本身不含画面。预览时由 hls.js 逐片取流，所以能播；
而「下载」拿到的仍是这份纯文本清单，存到硬盘上用任何播放器都打不开。

对策：把下载改造成一次服务端 ffmpeg 合并——

    ffmpeg -headers "<提取时的防盗链头>" -i <原始 m3u8 直链> -c copy -f mp4 <临时文件>

``-c copy`` 只转封装、不重编码，耗时基本等于「把视频下载一遍」；
分片的相对地址、``#EXT-X-KEY`` 密钥 URI 都由 ffmpeg 按远端 base 自行解析；
master playlist 会自动挑选最高的一个变体。

两个必须遵守的细节：
  1. 喂给 ffmpeg 的必须是**原始直链**（``sources[i].direct_url``），不能是改写后的代理清单：
     代理清单里的分片是 ``/api/stream?...`` 相对路径，脱离本服务就是废地址。
  2. 防盗链头要从 :data:`app.stream_proxy.headers_jar` 里取回来，拼成 ffmpeg 的
     ``-headers``（``Name: value`` 行以 CRLF 分隔），否则 B 站/抖音类源站直接回 403。

安全与资源：入口沿用 ``/api/stream`` 的 HMAC 令牌 + SSRF 校验（``open_target``）；
产物路径只留在本模块内存里，绝不进任务结果（否则等于对外暴露服务器文件系统路径）；
``REMUX_CONCURRENCY`` 限并发，``REMUX_MAX_BYTES`` 限体积，``sweep()`` 清孤儿文件。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from .config import config
from .extractor import ExtractError
from .jobs import DONE, RUNNING, STEPS_REMUX, jobs
from .playlist import analyze
from .stream_proxy import headers_jar

log = logging.getLogger("video-lens.remux")

# 产物文件名前缀（sweep 依此识别本模块的文件，绝不误删别人的东西）
_PREFIX = "remux-"

_exe: str | None = None
_probed = False
_lock = threading.Lock()
_slots = threading.Semaphore(max(1, config.REMUX_CONCURRENCY))
_outputs: dict[str, str] = {}        # job_id → 产物绝对路径（不出后端）
_names: dict[str, str] = {}         # job_id → 用户看到的下载文件名
_estimates: dict[str, int] = {}     # job_id → 预估产物体积（清单分析得出）
_durations: dict[str, float] = {}   # job_id → 总时长（进度分母）


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------

def probe_ffmpeg() -> str | None:
    """定位 ffmpeg 可执行文件（``VX_FFMPEG_PATH`` 可指向绝对路径）。只探一次并缓存。"""
    global _exe, _probed
    if not _probed:
        _exe = shutil.which(config.FFMPEG_PATH) or _search_common()
        _probed = True
    return _exe


def _search_common() -> str | None:
    """PATH 里找不到时，再试几个装机惯例位置（winget 链接目录 / 手工解压目录）。"""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "AppData", "Local", "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.join("C:\\", "ffmpeg", "bin", "ffmpeg.exe"),
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for path in candidates:
        try:
            if path and os.path.isfile(path) and os.access(path, os.X_OK if os.name != "nt" else os.F_OK):
                return path
        except OSError:  # pragma: no cover —— 极端权限环境下静默跳过
            continue
    return None


def available() -> bool:
    """能力位：本机是否能做 HLS→MP4 合并（前端据此决定「下载」按钮的行为）。"""
    return probe_ffmpeg() is not None


# ---------------------------------------------------------------------------
# 文件名 / 临时目录
# ---------------------------------------------------------------------------

_BAD_FN = re.compile(r'[\\/:*?"<>|\r\n\t\x00]')


def safe_filename(raw: str) -> str:
    """把前端传来的建议文件名洗成安全名（保留中文，去路径分隔符，强制 .mp4 后缀）。"""
    base = _BAD_FN.sub("_", (raw or "").strip())[:100].strip(" ._")
    if not base:
        base = "video"
    if not base.lower().endswith(".mp4"):
        base += ".mp4"
    return base


def _tmp_dir() -> str:
    os.makedirs(config.REMUX_TEMP_DIR, exist_ok=True)
    return config.REMUX_TEMP_DIR


def _job_id_of(filename: str) -> str:
    """``remux-<jobid>.mp4`` → ``<jobid>``。"""
    stem = filename[len(_PREFIX):] if filename.startswith(_PREFIX) else filename
    return stem.split(".", 1)[0]


# ---------------------------------------------------------------------------
# 任务入口（由 /api/remux 调用）
# ---------------------------------------------------------------------------

def start(raw_url: str, filename: str = "", *, sweep_first: bool = True) -> dict[str, Any]:
    """建一个合并任务并交给后台线程执行，立即返回 ``{job_id, expected_ms}``。

    ``raw_url`` 必须已经过 :func:`app.stream_proxy.open_target` 的令牌 + SSRF 校验。

    ``sweep_first``：批量场景下应传 ``False``。清扫是按"任务是否 DONE"判断孤儿的，
    而批量里各条目的合并任务几乎同时创建，先建的还在跑、后建的触发清扫时看到的是一个
    **刚创建、产物尚未登记**的任务，容易把正在写的文件当成孤儿删掉（Windows 上表现为
    ``WinError 32 文件被占用``，产物被误删后 zip 里就少了这一条）。
    """
    if probe_ffmpeg() is None:
        raise ExtractError(
            "本机未安装 ffmpeg，无法把 m3u8 合并成 MP4。"
            "安装后重启服务即可（Windows：winget install Gyan.FFmpeg）；"
            "或复制原始直链，交给本地播放器/VLC 下载。",
            "ffmpeg_missing",
        )
    if sweep_first:
        sweep()
    job = jobs.create(raw_url)
    with _lock:
        _names[job.job_id] = safe_filename(filename)
    # 合并可能长达十几分钟，看门狗必须用 REMUX_TIMEOUT，不能被解析的 EXTRACT_TIMEOUT 误杀
    asyncio.create_task(_supervise(job.job_id))
    log.info("remux job %s queued", job.job_id)
    return {"job_id": job.job_id, "expected_ms": config.REMUX_TIMEOUT * 1000}


async def _supervise(job_id: str) -> None:
    """看门狗：超时即把任务判失败并清理产物（线程里的 ffmpeg 也会自行退出）。"""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(jobs.run, job_id, lambda u, p: _work(job_id, u, p),
                              STEPS_REMUX),
            timeout=config.REMUX_TIMEOUT,
        )
    except asyncio.TimeoutError:
        jobs.fail(job_id, "remux_timeout",
                  f"合并超时（超过 {config.REMUX_TIMEOUT} 秒）：源站限速或分片过多，"
                  f"可稍后重试，或复制直链用本地工具下载",
                  step=STEPS_REMUX["failed"])
        log.warning("remux job %s timed out", job_id)
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何逃逸异常都要落到任务上
        jobs.fail(job_id, "remux_failed", f"合并失败：{exc.__class__.__name__}")
        log.exception("remux job %s crashed", job_id)
    finally:
        prune(job_id)


def _headers_arg(url: str) -> str:
    """把暂存的防盗链头拼成 ffmpeg 的 ``-headers`` 取值（CRLF 分隔，空则不分）。"""
    jar = headers_jar.get(url)
    lines = [f"{k}: {v}" for k, v in jar.items() if v]
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# 线程内实际执行
# ---------------------------------------------------------------------------

def _work(job_id: str, url: str, progress: Callable[[str], None]) -> dict[str, Any]:
    exe = probe_ffmpeg()
    if exe is None:  # 理论上不会到这里（start 已拦），留作兜底
        raise ExtractError("本机未安装 ffmpeg，无法合并 HLS 流", "ffmpeg_missing")

    if not _slots.acquire(timeout=120):
        raise ExtractError(
            f"合并通道已满（同时最多 {config.REMUX_CONCURRENCY} 个任务），请稍后重试",
            "remux_busy",
        )
    try:
        progress("正在分析清单，估算体积…")
        # 先摸清单：拿到总时长（进度分母）与预估体积（下载前就能告诉用户多大）。
        # follow_master=False：喂进来的 direct_url 已是 yt-dlp 解析好的具体档位，
        # 再去主清单挑变体等于丢掉用户选的清晰度（详见 playlist.analyze 的说明）。
        info = analyze(url, follow_master=False)
        est = info.get("estimate_bytes")
        total_secs = info.get("duration")
        with _lock:
            _estimates[job_id] = est
            _durations[job_id] = total_secs
        if est:
            log.info("remux %s: 预估 %.1f MB / %.0fs", job_id, est / 1048576, total_secs or 0)

        progress("正在连接 HLS 源并下载分片…")
        path = os.path.join(_tmp_dir(), f"{_PREFIX}{job_id}.mp4")
        with _lock:
            _outputs[job_id] = path
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin"]
        head = _headers_arg(url)
        if head:
            cmd += ["-headers", head]
        cmd += [
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", url,
            "-c", "copy",                 # 只转封装，不重编码
            "-f", "mp4",
            "-movflags", "+faststart",     # moov 前置，下载中/边下边播都友好
            "-y", path,
        ]
        rc, tail, why = _spawn(cmd, path, job_id=job_id, estimate=est,
                               total_secs=total_secs)

        if rc != 0 or why == "oversize" or why == "timeout" or not os.path.isfile(path):
            drop(job_id)
            raise ExtractError(_explain(rc, tail, why), "remux_failed")

        size = _size(path)
        if size < 4096:                   # 空容器：清单里没有可用分片（或被加密）
            drop(job_id)
            raise ExtractError(
                "该 HLS 流没有可写出的画面数据：可能被加密（EXT-X-KEY）或只有音轨。"
                "可复制原始直链交给站点客户端/本地播放器尝试。", "remux_failed")

        # 看门狗可能已把任务判失败（客户端超时）—— 此时不留孤儿文件
        cur = jobs.get(job_id)
        if cur is None or cur.status != RUNNING:
            drop(job_id)
            return {}

        progress("合并完成，正在准备保存…")
        with _lock:
            name = _names.get(job_id) or "video.mp4"
        log.info("remux job %s done: %s bytes", job_id, size)
        return {"kind": "remux", "mp4": True, "size_bytes": size, "filename": name,
                "duration": int(total_secs) if total_secs else None}
    finally:
        _slots.release()
        with _lock:
            _estimates.pop(job_id, None)
            _durations.pop(job_id, None)


def _spawn(cmd: list[str], path: str, *, job_id: str = "",
           estimate: int | None = None,
           total_secs: float | None = None) -> tuple[int, str, str | None]:
    """跑 ffmpeg，同时盯体积与超时，并**持续上报进度**。

    返回 ``(返回码, stderr 摘要, 中止原因)``。

    **进度怎么算**：ffmpeg 是边下分片边写 mp4 的，所以「已写字节数」就是真实进度。
    分母优先用清单分析出的预估体积（实测误差 ~3%，够画进度条）；拿不到预估时退回
    按已耗时给一个不确定的"进行中"文案，绝不编一个假的百分比。

    为什么不用 ffmpeg 的 ``-progress``：它输出的是"已处理时长"，而我们已经能从文件
    体积拿到更直观的信息，且不用再多解析一路管道（多一路读线程 = 多一处可能卡死）。

    用 list-argv + 无 shell，因此 URL / headers 里的引号不会被二次解析；
    stderr 单独一个线程读，避免管道写满把 ffmpeg 卡死。
    """
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(  # noqa: S603 —— argv 来自受控拼装，无 shell
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", creationflags=flags,
        )
    except OSError as exc:
        raise ExtractError(f"无法启动 ffmpeg：{exc}", "ffmpeg_missing") from exc

    lines: list[str] = []

    def drain() -> None:
        try:
            for line in proc.stderr or ():
                line = line.strip()
                if line and len(lines) < 200:
                    lines.append(line)
        except (OSError, ValueError):  # pragma: no cover —— 进程已退出时管道可能已关
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    deadline = time.monotonic() + max(30, config.REMUX_TIMEOUT - 15)
    started = time.monotonic()
    why: str | None = None
    last_report = 0.0
    while True:
        if proc.poll() is not None:
            break
        written = _size(path)
        if written > config.REMUX_MAX_BYTES:
            why = "oversize"
            _terminate(proc)
            break
        now = time.monotonic()
        if now > deadline:
            why = "timeout"
            _terminate(proc)
            break

        # 最多每 1.5 秒上报一次：任务状态是共享结构，刷太快只会让轮询白跑
        if job_id and now - last_report >= 1.5:
            last_report = now
            _report_progress(job_id, written, estimate, total_secs, now - started)
        time.sleep(0.5)

    reader.join(timeout=3)
    tail = " / ".join(lines[-3:])[:400]
    return proc.returncode if proc.returncode is not None else -1, tail, why


def _report_progress(job_id: str, written: int, estimate: int | None,
                     total_secs: float | None, elapsed: float) -> None:
    """把「已写/预估」写进任务状态，前端轮询即可画出百分比。

    百分比封顶 99%：最后还有 moov 收尾与磁盘落盘，没到 DONE 就不该显示 100%，
    否则进度条会先满、再等几秒才真的完成，观感上像卡住了。
    """
    if estimate and estimate > 0:
        pct = min(99, int(written * 100 / estimate))
        step = (f"正在合并 · {pct}%（{_mb(written)} / 约 {_mb(estimate)}）")
        jobs.update(job_id, step=step, progress=pct,
                    written_bytes=written, estimate_bytes=estimate)
    else:
        # 拿不到预估：只报"已写入多少 + 已耗时"，不编百分比
        jobs.update(job_id, step=f"正在合并 · 已写入 {_mb(written)}（已用 {int(elapsed)} 秒）",
                    progress=None, written_bytes=written, estimate_bytes=None)


def _mb(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _terminate(proc: subprocess.Popen[str]) -> None:
    """温和终止：先 terminate，给 ffmpeg 一点时间收尾（写 moov），2s 后强杀。"""
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except OSError:  # pragma: no cover
            pass


def _explain(rc: int, tail: str, why: str | None) -> str:
    """把 ffmpeg 的失败翻译成人话（保留一句原始报错，方便技术用户自查）。"""
    if why == "oversize":
        return (f"合并后的文件超过上限 {config.REMUX_MAX_BYTES // (1024 * 1024)} MB，已中止。"
                f"可复制原始直链用本地工具下载，或调大 VX_REMUX_MAX_BYTES 后重启服务。")
    if why == "timeout":
        return "合并超时：源站限速或分片过多。可稍后重试，或复制直链交给本地播放器下载。"
    hint = ""
    low = tail.lower()
    if "403" in low or "404" in low or "forbidden" in low or "not found" in low:
        hint = "源站拒绝了取流（直链多半已过期），请回到本页重新提取后再下载。"
    elif "ext-x-key" in low or "decrypt" in low or "aes" in low:
        hint = "该流带 EXT-X-KEY 加密，服务端没有密钥，无法合并。"
    elif "codec" in low or "muxer" in low or "global header" in low:
        hint = "该编码不适合直接封装进 MP4（如 HEVC/杜比视界/部分 fMP4 分片）。"
    return ("该 HLS 流无法直接合并为 MP4。" + (hint + " " if hint else "") +
            f"（ffmpeg 退出码 {rc}" + (f"：{tail[:160]}）" if tail else "）"))


# ---------------------------------------------------------------------------
# 产物存取与清理
# ---------------------------------------------------------------------------

def output_path(job_id: str) -> str | None:
    """任务已完成且文件仍在 → 返回绝对路径；否则 ``None``（前端据此提示重新下载）。"""
    with _lock:
        path = _outputs.get(job_id)
    if not path or not os.path.isfile(path):
        return None
    return path


def filename_of(job_id: str) -> str:
    with _lock:
        return _names.get(job_id) or "video.mp4"


def drop(job_id: str) -> None:
    """删除某任务的产物并注销登记（失败路径专用）。"""
    with _lock:
        path = _outputs.pop(job_id, None)
    if path:
        _remove(path)


def prune(job_id: str) -> None:
    """任务终态后调用：产物已完成则保留（等用户取），已失败/过期则删。"""
    job = jobs.get(job_id)
    if job is not None and job.status == DONE:
        return
    drop(job_id)
    with _lock:
        _names.pop(job_id, None)


def sweep() -> int:
    """清扫临时目录：任务已不存在、或产物超时未取走 → 删除。

    任务表是 LRU 淘汰的，被淘汰时不会回调本模块，所以只能靠这里定期回收；
    ``start()`` 与启动时各触发一次，正常用量下磁盘占用不会累积。

    **绝不删正在跑的任务的产物**：``jobs`` 里的任务只要还没 DONE 就可能是"正在写这个
    文件"，删它会让合并失败、批量里少一条（Windows 上还会撞文件占用）。因此除了
    "任务不存在"和"超时"两种情况，还要显式跳过 QUEUED/RUNNING 的任务。
    """
    from .jobs import QUEUED, RUNNING

    now = time.time()
    removed = 0
    target = config.REMUX_TEMP_DIR
    try:
        entries = os.listdir(target) if os.path.isdir(target) else []
    except OSError:
        return 0
    for fn in entries:
        if not fn.startswith(_PREFIX) or not fn.endswith(".mp4"):
            continue
        path = os.path.join(target, fn)
        job_id = _job_id_of(fn)
        job = jobs.get(job_id)
        # 正在排队/执行的合并任务：产物可能刚建好还没写完，跳过
        if job is not None and job.status in (QUEUED, RUNNING):
            continue
        try:
            stale = now - os.path.getmtime(path) > config.JOB_TTL
        except OSError:
            stale = True
        if job is None or job.status != DONE or stale:
            with _lock:
                _outputs.pop(job_id, None)
                _names.pop(job_id, None)
            if _remove(path):
                removed += 1
    if removed:
        log.info("swept %d stale remux artifact(s)", removed)
    return removed


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover —— 文件被播放器占用时留给下次清扫
        log.warning("cannot remove %s: %s", os.path.basename(path), exc)
        return False
