"""批量下载：一次提交多条链接 → 逐条提取（HLS 再合并）→ 打包成一个 zip 取走。

**为什么是"逐条 + 打包"而不是"并发下载再各自保存"**

浏览器一次只会弹一个下载框（多发几个只会被拦或被用户点掉一堆），所以批量下载的正确
交付形态是**一个 zip**。而 zip 要求所有条目都已完成，因此这里必须是个有终态的任务：
每条链接独立提取、独立失败，全部结束后统一打包。

**为什么条目之间串行度可控**

站点解析是重活（yt-dlp 要跑十几秒），并发太高会被目标站风控、也会把本机 CPU 打满。
``BATCH_CONCURRENCY`` 控制同时在跑的条目数，其余排队 —— 这样"提交 20 条"不会瞬间
触发 20 个并发外呼。

**失败隔离**

一条链接解析失败**不影响**其余条目：该项标记 failed 并记录原因，其余照常提取，
最终 zip 里只放成功的那些。打包结果里同时给出成功/失败清单，用户知道少了什么。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import zipfile
from typing import Any, Callable

from . import remux
from .config import config
from .extractor import ExtractError, extract
from .store import DONE, FAILED, RUNNING, store

log = logging.getLogger("video-lens.batch")

#: zip 产物文件名前缀（清扫依此识别本模块文件，不碰别人的东西）
_PREFIX = "batch-"

#: 打包用并发闸门：批量提取与合并都会吃网络带宽，同时开太多会被源站限速
_slots: asyncio.Semaphore | None = None

#: batch_id → zip 绝对路径（不出后端，与 remux 同一套"路径不进结果"的原则）
_archives: dict[str, str] = {}


def _limit() -> asyncio.Semaphore:
    """惰性创建信号量（必须在事件循环内创建，避免跨 loop 绑定）。"""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, config.BATCH_CONCURRENCY))
    return _slots


def _tmp_dir() -> str:
    os.makedirs(config.REMUX_TEMP_DIR, exist_ok=True)
    return config.REMUX_TEMP_DIR


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------

#: 从粘贴的文本里挑出链接。用户常从聊天记录整段粘过来，混着标题、序号、中文说明，
#: 因此这里"只挑得像 URL 的"，而不是要求每行都干净。
_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]，。；]+", re.I)


def parse_urls(text: str, limit: int | None = None) -> list[str]:
    """把一段文本切成 URL 列表（保序去重、截断到上限）。

    同时兼容两种输入：**多行链接**（每行一条）和**一大段混排文本**（自动挑出 URL）。
    这样用户不管是逐行粘还是整段复制都能用。
    """
    cap = limit or config.BATCH_MAX_URLS
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:，。；、）)】》」'\"")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def start(batch_id: str) -> None:
    """启动一个批次的处理（由 /api/batch 调用）。"""
    asyncio.create_task(_supervise(batch_id))


async def _supervise(batch_id: str) -> None:
    """整批看门狗 + 逐条调度。任何逃逸异常都要落到批次记录上，不能静默消失。"""
    try:
        await asyncio.wait_for(_run_batch(batch_id), timeout=config.BATCH_TIMEOUT)
    except asyncio.TimeoutError:
        _mark_pending_failed(batch_id, f"整批超时（超过 {config.BATCH_TIMEOUT} 秒）")
        log.warning("batch %s timed out", batch_id)
    except Exception as exc:  # noqa: BLE001
        _mark_pending_failed(batch_id, f"批量处理出错：{exc.__class__.__name__}")
        log.exception("batch %s crashed", batch_id)


def _mark_pending_failed(batch_id: str, message: str) -> None:
    batch = store.get_batch(batch_id)
    if not batch:
        return
    for it in batch.items:
        if it.status in ("pending", "running"):
            store.update_item(batch_id, it.item_id, status=FAILED, error=message)


async def _run_batch(batch_id: str) -> None:
    """并发跑所有条目（受信号量限制），全部结束后打包。"""
    batch = store.get_batch(batch_id)
    if not batch:
        return

    async def one(item_id: str, url: str) -> None:
        async with _limit():
            await _process_item(batch_id, item_id, url)

    await asyncio.gather(*(one(it.item_id, it.url) for it in batch.items))

    # 全部结束 → 打包（失败项不进 zip，但要记在结果里）。
    # 先置 packing：条目全落定与打包完成之间有窗口期，若不置位，前端会在这段时间
    # 认为"已完成"并弹出下载按钮，点击只能拿到 409/410。
    store.update_batch(batch_id, pack_state="packing")
    try:
        path, size, included = await asyncio.to_thread(_build_zip, batch_id)
        log.info("batch %s packed: %d file(s), %d bytes", batch_id, included, size)
        if size > config.BATCH_ZIP_MAX_BYTES:
            _drop_archive(batch_id)
            store.update_batch(batch_id, pack_state="empty",
                               pack_error="打包体积超过上限，已丢弃")
            _mark_pending_failed(batch_id, "打包体积超过上限，已丢弃")
        else:
            store.update_batch(batch_id, pack_state="packed", pack_error="")
    except ExtractError as exc:
        log.warning("batch %s pack skipped: %s", batch_id, exc)
        store.update_batch(batch_id, pack_state="empty", pack_error=str(exc))
        _mark_pending_failed(batch_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("batch %s pack failed", batch_id)
        store.update_batch(batch_id, pack_state="empty",
                           pack_error=f"打包失败：{exc.__class__.__name__}")
        _mark_pending_failed(batch_id, f"打包失败：{exc.__class__.__name__}")


async def _process_item(batch_id: str, item_id: str, url: str) -> None:
    """单条：提取 → 需要时合并 → 记录结果。失败只影响这一条。"""
    store.update_item(batch_id, item_id, status=RUNNING, error="")
    try:
        info = await asyncio.to_thread(extract, url)
    except ExtractError as exc:
        store.update_item(batch_id, item_id, status=FAILED, error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        store.update_item(batch_id, item_id, status=FAILED,
                          error=f"解析失败：{exc.__class__.__name__}")
        return

    sources = info.get("sources") or []
    store.update_item(
        batch_id, item_id,
        title=info.get("title") or "",
        provider=info.get("provider") or "",
        duration=info.get("duration"),
        source_count=len(sources),
        is_hls=bool(sources and sources[info.get("default_index", 0)].get("is_hls")),
    )
    if not sources:
        store.update_item(batch_id, item_id, status=FAILED, error="没有可下载的源")
        return

    src = sources[info.get("default_index", 0)]
    store.update_item(
        batch_id, item_id,
        size_estimate_bytes=src.get("size_estimate_bytes") or 0,
    )
    if not src.get("is_hls"):
        # 非 HLS：直链本身就是完整文件，打包时直接拉取即可，无需合并
        store.update_item(batch_id, item_id, status=DONE, size_bytes=src.get("size_bytes") or 0)
        return

    # HLS：必须服务端合并，否则打进 zip 的只是一份打不开的清单
    if not remux.available():
        store.update_item(batch_id, item_id, status=FAILED,
                          error="本机未安装 ffmpeg，无法合并 HLS 源")
        return
    try:
        await _remux_item(batch_id, item_id, src, info.get("title") or "")
    except ExtractError as exc:
        store.update_item(batch_id, item_id, status=FAILED, error=str(exc))


async def _remux_item(batch_id: str, item_id: str, src: dict[str, Any],
                      title: str) -> None:
    """复用 remux 模块把 HLS 条目合并成 MP4，并等它结束。

    ``sweep_first=False``：批量里多个条目几乎同时创建合并任务，若每次都触发清扫，
    后建的任务会把先建任务"刚登记、还没写完"的产物误判成孤儿删掉（真机上表现为
    zip 里少了几条）。
    """
    job = remux.start(src["direct_url"], _file_stem(title, item_id), sweep_first=False)
    store.update_item(batch_id, item_id, remux_job=job["job_id"])

    from .jobs import jobs

    deadline = time.monotonic() + config.REMUX_TIMEOUT + 30
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        cur = jobs.get(job["job_id"])
        if cur is None:
            raise ExtractError("合并任务已过期", "remux_failed")
        # 把合并进度同步到条目上，批量面板因此也能画进度条
        cur_pub = cur.public()
        store.update_item(batch_id, item_id,
                          progress=cur_pub.get("progress"),
                          size_bytes=cur_pub.get("written_bytes") or 0)
        if cur.status == DONE:
            # 任务 DONE 只代表 ffmpeg 退出码为 0，还要确认产物文件真的在 ——
            # 若被并发清扫误删，这里应如实报错，而不是记一条没有内容的"成功"。
            if remux.output_path(job["job_id"]) is None:
                raise ExtractError("合并产物已丢失，请重试该条", "remux_failed")
            store.update_item(batch_id, item_id, status=DONE, progress=None,
                              size_bytes=(cur.result or {}).get("size_bytes") or 0)
            return
        if cur.status == FAILED:
            raise ExtractError((cur.error or {}).get("message") or "合并失败", "remux_failed")
    raise ExtractError("合并超时", "remux_timeout")


def _file_stem(title: str, fallback: str) -> str:
    """给打包用的文件名主干：优先标题，其次条目 id。"""
    base = remux.safe_filename(title or fallback)
    return base[:-4] if base.lower().endswith(".mp4") else base


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------

def _build_zip(batch_id: str) -> tuple[str, int, int]:
    """把该批已完成条目打包成 zip，返回 ``(路径, 体积, 文件数)``。

    非 HLS 条目此刻才真正拉取直链（延迟下载：只在确定要交付时才耗流量）；
    HLS 条目直接取 remux 的产物。zip 内文件名去重，避免同名互相覆盖。
    """
    batch = store.get_batch(batch_id)
    if not batch:
        raise ExtractError("批次不存在", "batch_missing")

    path = os.path.join(_tmp_dir(), f"{_PREFIX}{batch_id}.zip")
    used: set[str] = set()
    count = 0

    import httpx
    from .stream_proxy import headers_jar

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for idx, it in enumerate(batch.items, 1):
            if it.status != DONE:
                continue
            name = _unique_name(_file_stem(it.title, it.item_id), idx, used, "mp4")
            if it.remux_job:
                artifact = remux.output_path(it.remux_job)
                if not artifact:
                    # 产物已被清扫：降级为跳过，不因为一条而让整包失败
                    store.update_item(batch_id, it.item_id, status=FAILED,
                                      error="合并产物已被清理")
                    continue
                zf.write(artifact, name)
                count += 1
                continue

            # 非 HLS：现拉现写，边下边入 zip（不落临时文件，省一倍磁盘）
            try:
                headers = dict(headers_jar.get(it.url)) or None
                with httpx.Client(timeout=60, follow_redirects=True,
                                  headers=headers) as client:
                    with client.stream("GET", it.url) as resp:
                        resp.raise_for_status()
                        with zf.open(zipfile.ZipInfo(name), "w") as dst:
                            written = 0
                            for chunk in resp.iter_bytes(256 * 1024):
                                written += len(chunk)
                                if written > config.BATCH_ZIP_MAX_BYTES:
                                    raise ExtractError("单个文件超过打包上限", "oversize")
                                dst.write(chunk)
                store.update_item(batch_id, it.item_id, size_bytes=written)
                count += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不影响整包
                log.warning("batch %s item %s fetch failed: %s", batch_id, it.item_id, exc)
                store.update_item(batch_id, it.item_id, status=FAILED,
                                  error=f"下载失败：{exc.__class__.__name__}")

    if count == 0:
        _remove(path)
        raise ExtractError("该批次没有任何可打包的内容", "batch_empty")

    _archives[batch_id] = path
    return path, os.path.getsize(path), count


def _unique_name(stem: str, idx: int, used: set[str], ext: str) -> str:
    """给 zip 内的文件起一个不重复的名字（``01_标题.mp4``）。"""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", stem)[:80].strip(" ._") or "video"
    name = f"{idx:02d}_{safe}.{ext}"
    n = 2
    while name in used:
        name = f"{idx:02d}_{safe}_{n}.{ext}"
        n += 1
    used.add(name)
    return name


# ---------------------------------------------------------------------------
# 产物存取与清理
# ---------------------------------------------------------------------------

def archive_path(batch_id: str) -> str | None:
    path = _archives.get(batch_id)
    if not path or not os.path.isfile(path):
        return None
    return path


def filename_of(batch_id: str) -> str:
    batch = store.get_batch(batch_id)
    label = (batch.label if batch else "") or "batch"
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", label)[:60].strip(" ._") or "batch"
    return f"{safe}.zip"


def _drop_archive(batch_id: str) -> None:
    path = _archives.pop(batch_id, None)
    if path:
        _remove(path)


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover
        log.warning("cannot remove %s: %s", os.path.basename(path), exc)
        return False


def sweep() -> int:
    """清扫孤儿 zip：批次已不存在、或文件超过 TTL 未被取走。"""
    now = time.time()
    removed = 0
    target = config.REMUX_TEMP_DIR
    try:
        entries = os.listdir(target) if os.path.isdir(target) else []
    except OSError:
        return 0
    for fn in entries:
        if not fn.startswith(_PREFIX) or not fn.endswith(".zip"):
            continue
        path = os.path.join(target, fn)
        batch_id = fn[len(_PREFIX):-4]
        batch = store.get_batch(batch_id)
        try:
            stale = now - os.path.getmtime(path) > config.JOB_TTL
        except OSError:
            stale = True
        if batch is None or stale:
            _archives.pop(batch_id, None)
            if _remove(path):
                removed += 1
    if removed:
        log.info("swept %d stale batch archive(s)", removed)
    return removed
