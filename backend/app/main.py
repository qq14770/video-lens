"""FastAPI 装配层 —— 路由、中间件、静态托管。

对外接口（全部 JSON，前端零依赖框架即可对接）：

    GET  /                     → 单页 UI（frontend/index.html）
    GET  /api/health           → 存活探针 + 能力位（是否装 curl_cffi、是否带 Cookie、能否合并 HLS）
    POST /api/extract          → 提交解析任务        {"url": "..."} → {"job_id": "..."}
    GET  /api/jobs/{job_id}    → 轮询任务进度与结果（解析与合并共用）
    GET  /api/stream?u=&t=     → 直链反向代理（Range 透传 + m3u8 改写）
    POST /api/remux?u=&t=      → 把 m3u8 直链在服务端合并成 MP4（下载用），返回 job_id
    GET  /api/remux/{job_id}/file → 取走合并好的 MP4（附件下载）

批量下载（多链接 → 逐条提取/合并 → 打包一个 zip）：

    POST   /api/batch          → {"text": "多行链接"} → {"batch_id": ...}
    GET    /api/batch/{id}     → 轮询整批进度与逐条结果
    GET    /api/batch/{id}/file→ 取走 zip（打包未完成时 409）
    DELETE /api/batch/{id}     → 删除批次记录

定时任务（服务端持久化，重启不丢）：

    POST   /api/schedules      → 建计划（daily / interval）
    GET    /api/schedules      → 列出全部计划与下次触发时间
    PATCH  /api/schedules/{id} → 启停 / 改时间
    POST   /api/schedules/{id}/run → 立即执行一次
    DELETE /api/schedules/{id} → 删除计划

前端与后端同源部署（本进程直接托管 frontend/ 目录），因此生产环境无需额外 CORS；
若前端单独跑 ``pnpm dev``/静态服务器，请用 ``VX_ALLOW_ORIGIN`` 声明来源。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from . import batch, remux, scheduler
from .config import config
from .extractor import ExtractError, extract
from .jobs import DONE, jobs
from .security import UrlError, check_url
from .store import PENDING, Schedule, store
from .stream_proxy import (ProxyError, fetch_manifest, head_info, headers_jar,
                           looks_like_manifest, open_target, response_headers,
                           rewrite_manifest, stream)

logging.basicConfig(
    level=os.getenv("VX_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("video-lens")

app = FastAPI(
    title="Video Lens —— 视频内容提取",
    version=__version__,
    description="只返回标题、播放器与视频直链；广告与冗余字段在服务器端即被丢弃。",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------

if os.getenv("VX_ALLOW_ORIGIN", "").strip():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in os.environ["VX_ALLOW_ORIGIN"].split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class RateLimiter:
    """极简滑动窗口限流：每 IP 每分钟 ``config.RATE_LIMIT_RPM`` 次「提取」调用。

    只限提取，不限播放代理 —— 播放器拖动进度会产生大量 Range 请求，属正常行为。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        win = self._hits[key]
        while win and now - win[0] > 60:
            win.popleft()
        if len(win) >= config.RATE_LIMIT_RPM:
            return False
        win.append(now)
        return True


_limiter = RateLimiter()


@app.on_event("startup")
async def _on_startup() -> None:
    """启动顺序有讲究：

    1. 先自检可写目录 —— NAS/容器里最常见的故障是挂载目录属主不对，
       若等到用户点下载才报错，排查成本高得多；
    2. 再 :func:`app.store.Store.load` 把磁盘状态读进内存 —— 调度器与批量都要用它；
    3. 收拾"上次进程死在半路"的计划（``running`` 永远不会自己变回终态）；
    4. 启动调度器（它会立即扫描已到点的计划，所以必须在 load 之后）；
    5. 顺手清扫上次运行遗留的临时产物（zip / mp4），避免长期挂机累积。
    """
    _preflight_writable()
    _warn_secret()
    store.load()
    scheduler.reconcile_stale_runs()
    scheduler.start()
    try:
        removed = remux.sweep() + batch.sweep()
        if removed:
            log.info("startup cleanup: removed %d stale artifact(s)", removed)
    except Exception:  # noqa: BLE001 —— 清扫失败不该阻止服务启动
        log.exception("startup sweep failed")


def _warn_secret() -> None:
    """密钥自检。

    三种情况分级处理，因为后果差别很大：

      * **用了模板占位值** → 最危险。占位值是公开的，任何人拿它都能伪造出
        合法的播放/下载令牌，等于 SSRF 防线失效。必须醒目告警。
      * **没设** → 每次重启随机生成，旧链接集体失效。对自用是"预期行为"，
        但部署到 NAS 上会让人以为服务坏了，所以给一句说明。
      * **设了正常值** → 静默。
    """
    if config.SECRET_IS_PLACEHOLDER:
        log.error(
            "VX_SECRET 仍是模板里的占位值 —— 这是**公开密钥**，任何人都能伪造"
            "播放/下载链接，请立即更换！\n"
            "  → 生成： openssl rand -hex 32\n"
            "  → 写入 docker-compose.yml 的 VX_SECRET，然后 docker compose up -d"
        )
    elif not config.SECRET_FROM_ENV:
        log.warning(
            "未设置 VX_SECRET：本次启动使用随机密钥。服务重启后所有播放/下载链接"
            "会失效（安全上没问题，但体验上会以为坏了）。\n"
            "  → 长期运行建议固定它： openssl rand -hex 32  然后写入 VX_SECRET"
        )


def _preflight_writable() -> None:
    """确认两个持久化目录可写；不可写时给出**可直接照做**的处置建议。

    容器/NAS 场景下这一步很值：
      * Docker 卷的属主与容器内 uid 不一致 → ``PermissionError``；
      * 飞牛上把宿主目录映射进来但没给写权限 → 同样如此。

    这里只告警不退出：让服务起来、其余功能可用，比直接崩掉更友好；
    但日志必须把"哪个目录、为什么、怎么办"写清楚。

    处置建议按**当前运行身份**给：以 root 运行时再让人 chown 是多余的，
    照抄一条没用的命令只会更困惑。
    """
    is_root = hasattr(os, "getuid") and os.getuid() == 0

    targets = {
        "定时计划与批次状态": os.path.dirname(os.path.abspath(config.STORE_FILE)),
        "合并/打包临时产物": config.REMUX_TEMP_DIR,
    }
    for label, path in targets.items():
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write-probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            if is_root:
                # root 都写不进去，基本不是属主问题，chown 也救不了
                hint = ("  → 当前以 root 运行仍不可写，通常不是属主问题：\n"
                        "     请确认该目录不是以 :ro 只读挂载，且宿主文件系统未满/未只读")
            else:
                hint = ("  → Docker/飞牛：确认挂载的宿主目录属主与容器内 uid 一致"
                        "（容器内是 1000）：\n"
                        "     chown -R 1000:1000 <你映射的宿主目录>\n"
                        "  → 或改用 root 运行（compose 里注释掉 user: 那一行），即无需 chown")
            log.error("目录不可写（%s）：%s —— %s\n%s", label, path, exc, hint)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await scheduler.stop()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """统一给所有响应加基础安全头，并给 API 响应关掉缓存。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    """输入只收一个字段：分享链接。"""

    url: str = Field(min_length=4, max_length=config.MAX_URL_LEN,
                     description="视频分享链接（支持 B 站/抖音/YouTube/X 等）")


class BatchRequest(BaseModel):
    """批量输入：接受多行链接，也接受一整段混排文本（会自动挑出其中的 URL）。"""

    text: str = Field(default="", max_length=200_000,
                      description="多行视频链接（也可直接粘一大段含链接的文本）")
    urls: list[str] | None = Field(default=None, max_length=config.BATCH_MAX_URLS,
                                   description="也可以直接给 URL 数组")
    url: str | None = Field(default=None, max_length=config.MAX_URL_LEN,
                            description="单条链接（与 /api/extract 同名，方便照抄）")
    label: str = Field(default="", max_length=60, description="批次备注名（用作 zip 文件名）")


class ScheduleRequest(BaseModel):
    """定时计划：``daily`` 用 ``at``（HH:MM），``interval`` 用 ``every_minutes``。

    链接来源三种写法都接受（``urls`` 数组 / ``text`` 多行 / 单条 ``url``）：
    ``/api/extract`` 用的是 ``url`` 单数字段，调用方很自然会照抄到这里，
    若只认 ``text`` 就会得到一个"没有识别到任何链接"的费解报错。
    """

    text: str = Field(default="", max_length=200_000)
    urls: list[str] | None = Field(default=None, max_length=config.BATCH_MAX_URLS)
    url: str | None = Field(default=None, max_length=config.MAX_URL_LEN,
                            description="单条链接（与 /api/extract 同名，方便照抄）")
    mode: str = Field(default="daily", pattern="^(daily|interval)$")
    at: str = Field(default="22:00", max_length=5, description="daily 模式的触发时刻 HH:MM")
    every_minutes: int = Field(default=60, ge=1, le=10080, description="interval 模式的间隔分钟")
    label: str = Field(default="", max_length=60)
    enabled: bool = True


class SchedulePatch(BaseModel):
    """只允许改这几项；未传的字段保持原值。"""

    enabled: bool | None = None
    at: str | None = Field(default=None, max_length=5)
    every_minutes: int | None = Field(default=None, ge=1, le=10080)
    mode: str | None = Field(default=None, pattern="^(daily|interval)$")
    label: str | None = Field(default=None, max_length=60)


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------


@app.post("/api/extract", status_code=202)
async def submit_extract(payload: ExtractRequest, request: Request) -> dict[str, Any]:
    """校验链接 → 建任务 → 丢后台线程池解析 → 立即返回 ``job_id``。"""
    client_ip = request.client.host if request.client else "anon"
    if not _limiter.allow(client_ip):
        raise HTTPException(429, f"请求过于频繁，请稍后再试（限速 {config.RATE_LIMIT_RPM}/分钟）")

    try:
        url = check_url(payload.url)
    except UrlError as exc:
        raise HTTPException(400, str(exc)) from exc

    job = jobs.create(url)
    # 解析是阻塞型（yt-dlp 同步），用 to_thread 隔离，不拖慢事件循环；
    # 再套一层 asyncio.wait_for 作看门狗：站点卡死时把任务判失败，前端能立刻拿到反馈。
    asyncio.create_task(_supervise(job.job_id))
    log.info("job %s queued for %s", job.job_id, url[:80])
    # expected_ms 供前端把进度条映射到合理的时间轴（避免"假精确"）
    return {"job_id": job.job_id, "expected_ms": config.EXTRACT_TIMEOUT * 1000}


async def _supervise(job_id: str) -> None:
    """看门狗：超过 EXTRACT_TIMEOUT 仍未结束即标记失败（超时后线程自行退出）。"""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(jobs.run, job_id, _work),
            timeout=config.EXTRACT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        jobs.fail(job_id, "timeout",
                  f"解析超时（超过 {config.EXTRACT_TIMEOUT}s）：站点响应过慢或连接被拦截")
        log.warning("job %s timed out", job_id)
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何逃逸异常都要落到任务上
        jobs.fail(job_id, "internal_error", f"服务内部错误：{exc.__class__.__name__}")
        log.exception("job %s crashed", job_id)


def _work(url: str, progress: Any) -> dict[str, Any]:
    """线程内执行的解析包装（签名需与 :meth:`app.jobs.JobTable.run` 的 work 约定一致）。"""
    return extract(url, progress)


@app.get("/api/jobs/{job_id}")
async def poll_job(job_id: str) -> dict[str, Any]:
    """前端轮询：``{status, step, result|error}``（解析任务与合并任务共用）。"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期，请重新提取")
    return job.public()


@app.post("/api/remux", status_code=202)
async def submit_remux(
    u: str = Query(..., description="base64url 编码的原始 m3u8 直链（与 /api/stream 同源参数）"),
    t: str | None = Query(None, description="HMAC 签名令牌"),
    filename: str = Query("", max_length=120, description="建议的保存文件名"),
) -> dict[str, Any]:
    """把 m3u8 直链在服务端合并成一个 MP4 文件（下载用）。

    为什么需要它：m3u8 只是「分片清单」，纯文本存到硬盘上任何播放器都打不开 ——
    能预览是因为浏览器里的 hls.js 在逐片取流。下载必须先在服务端合并。

    与 ``/api/extract`` 一样是「任务 + 轮询」：立即返回 ``job_id``，前端轮询
    ``/api/jobs/{job_id}``，完成后把「下载」按钮指向 ``/api/remux/{job_id}/file``。
    """
    try:
        raw = open_target(u, t)              # 令牌校验 + SSRF 校验，与播放代理同一口径
    except ProxyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return remux.start(raw, filename)


@app.get("/api/remux/{job_id}/file")
async def download_remux(job_id: str) -> FileResponse:
    """取走合并好的 MP4。

    可重复下载（断点续传失败/浏览器拦截后还能再点一次）：产物保留到任务 TTL 过期，
    由 ``remux.sweep()`` 在下一次发起合并时回收，不在这里即删。
    """
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "合并任务不存在或已过期，请重新发起下载")
    if job.status != DONE:
        raise HTTPException(409, "合并尚未完成，请稍后再试")
    path = remux.output_path(job_id)
    if path is None:
        raise HTTPException(410, "合并产物已被清理，请重新发起下载")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers=_disposition(1, remux.filename_of(job_id), "mp4"),
    )


# ---------------------------------------------------------------------------
# 批量下载：多链接 → 逐条提取/合并 → 打包一个 zip
# ---------------------------------------------------------------------------


def _require_urls(text: str, urls: list[str] | None,
                  strict: bool = False, single: str | None = None
                  ) -> tuple[list[str], list[tuple[str, str]]]:
    """把请求里的链接归一化，返回 ``(可用链接, [(被拒链接, 原因), ...])``。

    接受三种写法（优先级：数组 > 单条 > 多行文本），因为它们分别对应
    "程序调用"、"照抄 /api/extract"、"用户从聊天记录整段粘贴" 三种真实场景。

    **不因为一条坏链接就拒绝整批**：批量场景下用户常从聊天记录整段粘贴，混进一两条
    失效或内网地址很正常；整批 400 会逼着他去逐条排查。这里改为把不合格的挑出来
    单独告知，其余照常处理。``strict=True`` 时（定时计划）才要求全部合格 ——
    计划是长期反复执行的，不该把必然失败的链接固化进去。
    """
    if urls:
        raw = list(urls)
    elif single and single.strip():
        raw = [single.strip()]
    else:
        raw = batch.parse_urls(text)
    if not raw:
        raise HTTPException(400, "没有识别到任何链接（请粘贴 http/https 开头的视频链接）")
    if len(raw) > config.BATCH_MAX_URLS:
        raise HTTPException(
            400, f"一次最多 {config.BATCH_MAX_URLS} 条链接（当前 {len(raw)} 条），请分批提交")

    checked: list[str] = []
    rejected: list[tuple[str, str]] = []
    for one in raw:
        try:
            checked.append(check_url(one))
        except UrlError as exc:
            rejected.append((one, str(exc)))

    if strict and rejected:
        bad = "、".join(u[:50] for u, _ in rejected[:3])
        raise HTTPException(400, f"有 {len(rejected)} 条链接不可用（{bad}…），请修正后再提交")
    if not checked:
        raise HTTPException(400, f"全部 {len(rejected)} 条链接都不可用：{rejected[0][1]}")
    return checked, rejected


@app.post("/api/batch", status_code=202)
async def submit_batch(payload: BatchRequest, request: Request) -> dict[str, Any]:
    """提交一批链接。立即返回 ``batch_id``，前端轮询 ``/api/batch/{id}`` 看进度。

    为什么限流口径按"条"而不是按"次"：一次提交 20 条链接对外就是 20 次站点请求，
    按次限流等于给了个绕过的口子。这里按链接数计入同一滑动窗口。
    """
    urls, rejected = _require_urls(payload.text, payload.urls, single=payload.url)

    client_ip = request.client.host if request.client else "anon"
    for _ in range(len(urls)):
        if not _limiter.allow(client_ip):
            raise HTTPException(
                429, f"请求过于频繁：本批 {len(urls)} 条链接超出限速 "
                     f"{config.RATE_LIMIT_RPM}/分钟，请稍后重试或减少条数")

    b = store.create_batch(urls, label=payload.label, rejected=rejected)
    batch.start(b.batch_id)
    log.info("batch %s queued: %d ok, %d rejected", b.batch_id, len(urls), len(rejected))
    return {
        "batch_id": b.batch_id,
        "total": len(b.items),
        "rejected": len(rejected),
        "expected_ms": config.EXTRACT_TIMEOUT * 1000 * max(1, len(urls) // max(1, config.BATCH_CONCURRENCY)),
    }


@app.get("/api/batch/{batch_id}")
async def poll_batch(batch_id: str) -> dict[str, Any]:
    """轮询整批进度：``{total, counts, finished, items:[…]}``。"""
    b = store.get_batch(batch_id)
    if b is None:
        raise HTTPException(404, "批次不存在或已过期")
    data = b.public(with_items=True)
    # 打包产物就绪时告诉前端可以取件了（路径本身不出后端）
    data["archive_ready"] = batch.archive_path(batch_id) is not None
    return data


@app.get("/api/batch/{batch_id}/file")
async def download_batch(batch_id: str) -> FileResponse:
    """取走打包好的 zip。

    三种未就绪的情况分开报，前端才能给出准确文案：
      * 条目还在跑 → 409「尚未全部结束」；
      * 条目结束但正在打包 → 409「正在打包」；
      * 打包完成但产物被清理 → 410。
    """
    b = store.get_batch(batch_id)
    if b is None:
        raise HTTPException(404, "批次不存在或已过期")
    state = b.public(with_items=False)
    if not state["items_settled"]:
        raise HTTPException(409, "批量任务尚未全部结束，请稍后再试")
    if state.get("pack_state") == "packing":
        raise HTTPException(409, "正在打包，请稍候几秒再点下载")
    path = batch.archive_path(batch_id)
    if path is None:
        why = state.get("pack_error") or "该批次没有可下载的内容"
        raise HTTPException(410, f"打包产物不可用：{why}")
    return FileResponse(
        path,
        media_type="application/zip",
        headers=_disposition(1, batch.filename_of(batch_id), "zip"),
    )


@app.delete("/api/batch/{batch_id}")
async def delete_batch(batch_id: str) -> dict[str, Any]:
    """删除批次记录（同时清掉 zip 产物）。"""
    if not store.delete_batch(batch_id):
        raise HTTPException(404, "批次不存在")
    return {"ok": True}


@app.get("/api/batches")
async def list_batches() -> dict[str, Any]:
    """最近的批次列表（不含逐条明细，供历史面板用）。"""
    return {"batches": store.list_batches()}


# ---------------------------------------------------------------------------
# 定时任务（服务端持久化）
# ---------------------------------------------------------------------------


@app.post("/api/schedules", status_code=201)
async def create_schedule(payload: ScheduleRequest) -> dict[str, Any]:
    """建一个定时计划。首次触发时间立即算好并落盘，重启后照常生效。"""
    urls, _ = _require_urls(payload.text, payload.urls, strict=True, single=payload.url)
    sched = scheduler.add_schedule(
        urls=urls, mode=payload.mode, at=payload.at,
        every_minutes=payload.every_minutes, label=payload.label, enabled=payload.enabled)
    return sched.public()


@app.get("/api/schedules")
async def list_schedules() -> dict[str, Any]:
    return {
        "schedules": store.list_schedules(),
        "enabled": config.SCHEDULE_ENABLED,
        "max_urls": config.BATCH_MAX_URLS,
    }


@app.patch("/api/schedules/{schedule_id}")
async def patch_schedule(schedule_id: str, payload: SchedulePatch) -> dict[str, Any]:
    """改计划：启停 / 改时刻 / 改间隔 / 改备注。

    改完必须**重算下次触发时间**，否则会出现"改成 22:00 但下次仍按旧的 08:00 跑"
    这种让人无法信任定时功能的诡异行为。
    """
    sched = store.get_schedule(schedule_id)
    if sched is None:
        raise HTTPException(404, "计划不存在")

    changes = payload.model_dump(exclude_none=True)
    if not changes:
        return sched.public()

    for key, value in changes.items():
        setattr(sched, key, value)
    sched.next_run_at = scheduler.compute_next(sched) if sched.enabled else None
    store.update_schedule(schedule_id, **changes, next_run_at=sched.next_run_at)
    return store.get_schedule(schedule_id).public()


@app.post("/api/schedules/{schedule_id}/run", status_code=202)
async def run_schedule_now(schedule_id: str) -> dict[str, Any]:
    """立即执行一次（不影响既定的下次时间）。"""
    if not await scheduler.run_now(schedule_id):
        raise HTTPException(409, "计划不存在或正在执行中")
    return {"ok": True, "message": "已开始执行，可在批次列表查看结果"}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str) -> dict[str, Any]:
    if not store.delete_schedule(schedule_id):
        raise HTTPException(404, "计划不存在")
    return {"ok": True}


@app.api_route("/api/stream", methods=["GET", "HEAD"])
async def stream_proxy(
    request: Request,
    u: str = Query(..., description="base64url 编码的原始直链"),
    t: str | None = Query(None, description="HMAC 签名令牌"),
    dl: int = Query(0, description="1 = 以附件形式下载（Content-Disposition: attachment）"),
    filename: str = Query("", max_length=120, description="下载时的文件名（仅 dl=1 生效）"),
):
    """直链反向代理。

    分支：
      * ``m3u8`` 清单 → 取全文、按行改写资源地址为代理路径、以清单类型返回；
      * 其余字节流 → Range 透传 + 流式转发，支持进度条拖动；
      * ``dl=1``   → 改为附件下载（前端「下载」按钮）；
      * ``HEAD``   → 只探元信息（播放器/下载器在真正取流前普遍先 HEAD 一次）。
    """
    try:
        raw = open_target(u, t)
    except ProxyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    is_head = request.method == "HEAD"
    if is_head:
        # HEAD 不回源拉流，只探一次元信息（播放器/下载器在真正取流前普遍先 HEAD）。
        #   probe 为 None  → 站点不支持 HEAD/网络异常：给 200 空壳，播放器随后直发 GET；
        #   probe >= 400  → 与 GET 分支同口径报 502，免得「HEAD 说行、GET 拉黑」的错觉。
        probe = await head_info(raw, dict(headers_jar.get(raw)))
        if probe is not None and probe.status_code >= 400:
            raise HTTPException(502, f"源站返回 {probe.status_code}（直链可能已过期，请重新提取）")
        headers = (response_headers(probe) if probe is not None else
                   {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream",
                    "Cache-Control": "private, max-age=60"})
        headers.update(_disposition(dl, filename, _ext_of(raw)))
        return Response(status_code=200, headers=headers)

    # ① HLS 清单
    if looks_like_manifest(raw):
        try:
            text = await fetch_manifest(raw)
        except ProxyError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        if "#EXTM3U" in text[:2048]:
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            headers.update(_disposition(dl, filename, "m3u8"))
            if dl:
                # 下载模式：返回**未改写**的原始清单 —— 这是降级路径（本机没装 ffmpeg 时
                # 前端仍会用它）。存成文件后可被外部播放器/VLC 打开，而改写后的代理清单
                # 只在本服务存活期间有效，落到硬盘上就是废文件。装了 ffmpeg 时前端不会
                # 走到这里，而是先经 /api/remux 合并成 MP4 再保存。
                return Response(content=text, media_type="application/x-mpegurl", headers=headers)
            return Response(content=rewrite_manifest(text, raw),
                            media_type="application/vnd.apple.mpegurl", headers=headers)
        # 少数站点用 .m3u8 结尾却返回 mp4 二进制 —— 走下面的字节流分支

    # ② 普通字节流：先尽力拿 Content-Type，再流式转发
    content_type = ""
    probe = await head_info(raw, {})
    if probe is not None:
        content_type = probe.headers.get("content-type", "")
    byte_range = request.headers.get("range")
    try:
        resp, body = await stream(raw, byte_range)
    except ProxyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    headers = response_headers(resp)
    headers.update(_disposition(dl, filename, _ext_of(raw)))
    media = resp.headers.get("content-type") or content_type or "application/octet-stream"
    status = resp.status_code if resp.status_code in (200, 206) else 200
    return StreamingResponse(body, status_code=status, headers=headers, media_type=media)


def _ext_of(url: str) -> str:
    """从直链路径里猜扩展名，仅用于文件名兜底。"""
    import re as _re

    path = _re.sub(r"[?#].*$", "", url).rsplit("/", 1)[-1]
    return path.rsplit(".", 1)[-1][:6] if "." in path else "mp4"


def _disposition(dl: int, filename: str, fallback_ext: str) -> dict[str, str]:
    """下载模式：把响应改成附件，并对文件名做 ASCII 兜底 + RFC 5987 编码（支持中文）。"""
    if not dl:
        return {}
    from urllib.parse import quote

    name = (filename or "").strip()[:120] or f"video.{fallback_ext}"
    ascii_name = re.sub(r"[^A-Za-z0-9._\-]", "_", name) or f"video.{fallback_ext}"
    return {
        "Content-Disposition": f'attachment; filename="{ascii_name}"; '
                               f"filename*=UTF-8''{quote(name)}"
    }


@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health() -> dict[str, Any]:
    """存活探针 + 当前能力位，方便部署后排障。"""
    try:
        import curl_cffi  # noqa: F401

        impersonate = True
    except Exception:  # noqa: BLE001
        impersonate = False
    return {
        "ok": True,
        "version": __version__,
        "impersonate_available": impersonate,
        "cookies_loaded": bool(os.getenv("VX_COOKIES_FILE", "").strip()),
        "rate_limit_rpm": config.RATE_LIMIT_RPM,
        # 前端据此决定 m3u8 的「下载」按钮走服务端合并（MP4）还是原样存清单（降级提示）
        "ffmpeg_available": remux.available(),
    }


# ---------------------------------------------------------------------------
# 静态托管（放在最后，避免吃掉 /api/*）
# ---------------------------------------------------------------------------


@app.api_route("/", include_in_schema=False, methods=["GET", "HEAD"])
async def index() -> FileResponse:
    """单页入口。"""
    target = os.path.join(config.STATIC_DIR, "index.html")
    if not os.path.isfile(target):
        raise HTTPException(500, f"未找到前端页面：{target}")
    return FileResponse(target, headers={"Cache-Control": "no-cache"})


if os.path.isdir(config.STATIC_DIR):
    app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")


@app.exception_handler(ExtractError)
async def _extract_error_handler(_req: Request, exc: ExtractError) -> JSONResponse:
    """直接调用（非任务化）场景下的兜底：把业务异常转成 JSON。"""
    return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=422)
