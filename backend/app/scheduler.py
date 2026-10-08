"""定时任务调度器：到点自动把计划里的链接跑一遍（提取 + 必要合并 + 打包）。

**为什么不用 cron / APScheduler**

本项目的定时需求很窄：``每天某时刻`` 或 ``每隔 N 分钟``，把几条链接重新提取一遍。
为此引入 cron 解析库或 APScheduler 属于给一个二十行的循环加一整套依赖。更重要的是
**可持久化**：计划要活过重启，而这需要与 :mod:`app.store` 的状态整合 —— 外部调度器
反而更难做到一致。

**调度语义（刻意保持可预测）**

* 每次 tick 扫描所有启用的计划，哪个 ``next_run_at`` 已到就触发；
* 触发后立刻算出**下一次**时间并落盘，所以进程在两次触发之间被杀掉也不会重复跑；
* 进程停机期间错过的计划：若错过时间在 ``SCHEDULE_MISSED_GRACE`` 内则补跑一次
  （笔记本合盖、临时重启这类场景应该补上），超过宽限期就跳过并记录原因
  （避免开机时把过去一周的每日任务连着跑十遍）；
* 单个计划失败不影响其它计划；异常一律落到计划的 ``last_message`` 上。

**并发**：调度器自己串行执行（一个计划跑完再跑下一个），但每个计划内部走
:mod:`app.batch` 的并发控制。这样"每天跑 10 条链接"不会和用户手动批量抢满带宽。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from .config import config
from .store import FAILED, PENDING, Schedule, store

log = logging.getLogger("video-lens.scheduler")

_task: asyncio.Task | None = None
_running = False
#: 正在执行的计划 id 集合：防止同一计划被重复触发
_inflight: set[str] = set()


# ---------------------------------------------------------------------------
# 时间计算
# ---------------------------------------------------------------------------

def compute_next(sched: Schedule, now: float | None = None) -> float:
    """算出该计划的下一次触发时间（时间戳）。

    * ``interval``：现在 + N 分钟（简单、可预测，不需要对齐整点）；
    * ``daily``：下一个 ``HH:MM``（今天该时刻已过就顺延到明天）。
    """
    now = now if now is not None else time.time()
    if sched.mode == "interval":
        return now + max(1, int(sched.every_minutes)) * 60

    hh, mm = _parse_at(sched.at)
    local = datetime.fromtimestamp(now)
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.timestamp()


def _parse_at(value: str) -> tuple[int, int]:
    """``"22:00"`` → ``(22, 0)``；非法输入退回 22:00（不因为手滑存坏值就永不触发）。"""
    try:
        hh_s, _, mm_s = (value or "").partition(":")
        hh, mm = int(hh_s), int(mm_s)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except (ValueError, TypeError):
        pass
    return 22, 0


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

def start() -> None:
    """启动后台调度循环（由 FastAPI startup 调用）。"""
    global _task
    if not config.SCHEDULE_ENABLED:
        log.info("scheduler disabled by VX_SCHEDULE_ENABLED")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info("scheduler started (tick=%ss)", config.SCHEDULE_TICK)


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
        log.info("scheduler stopped")


def add_schedule(urls: list[str], mode: str, at: str, every_minutes: int,
                 label: str = "", enabled: bool = True) -> Schedule:
    """建一个计划，并立即算出首次触发时间。"""
    sched = Schedule(
        schedule_id=uuid.uuid4().hex[:12],
        urls=urls,
        mode="interval" if mode == "interval" else "daily",
        at=at or "22:00",
        every_minutes=max(1, int(every_minutes or 60)),
        enabled=bool(enabled),
        label=label or f"{len(urls)} 条链接",
    )
    sched.next_run_at = compute_next(sched) if sched.enabled else None
    store.add_schedule(sched)
    log.info("schedule %s added: %s, next=%s", sched.schedule_id, sched.mode,
             _fmt(sched.next_run_at))
    return sched


def _fmt(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------

async def _loop() -> None:
    """周期性扫描。tick 间隔由 ``SCHEDULE_TICK`` 控制（默认 20s）。"""
    # 启动时补算：进程停机期间 next_run_at 可能已经过期，交给 _due() 判断补跑/跳过
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 循环绝不能因为一次异常而死掉
            log.exception("scheduler tick failed")
        await asyncio.sleep(max(5, config.SCHEDULE_TICK))


async def _tick() -> None:
    now = time.time()
    for sched in list(store.list_schedules()):
        obj = store.get_schedule(sched["schedule_id"])
        if obj is None or not obj.enabled or obj.schedule_id in _inflight:
            continue
        if obj.next_run_at is None:
            # 计划被启用但没算过时间（例如手工改过文件）→ 补算
            store.update_schedule(obj.schedule_id, next_run_at=compute_next(obj, now))
            continue
        if obj.next_run_at > now:
            continue

        # 已到点。先判断是否"错过太久"
        lateness = now - obj.next_run_at
        if lateness > config.SCHEDULE_MISSED_GRACE:
            nxt = compute_next(obj, now)
            store.update_schedule(
                obj.schedule_id, next_run_at=nxt, last_status="skipped",
                last_message=f"错过触发时间 {int(lateness // 60)} 分钟（超过补跑宽限），已跳过")
            log.info("schedule %s skipped (late by %ds)", obj.schedule_id, int(lateness))
            continue

        # 立刻排下一次，保证"触发即持久化"，中途被杀不会重复跑
        nxt = compute_next(obj, now)
        store.update_schedule(obj.schedule_id, next_run_at=nxt)
        asyncio.create_task(_fire(obj.schedule_id))


async def _fire(schedule_id: str) -> None:
    """执行一个计划：把它的链接当成一个批量任务跑。"""
    sched = store.get_schedule(schedule_id)
    if sched is None or schedule_id in _inflight:
        return
    _inflight.add(schedule_id)
    started = time.time()
    try:
        log.info("schedule %s firing (%d url(s))", schedule_id, len(sched.urls))
        # 立刻写下 last_run_at 与批次号：
        # 一次执行可能长达几十分钟（HLS 要逐条合并），若等到结束才记录，这期间
        # 界面上完全看不到"正在跑"，用户会以为定时没生效（实际反馈过这个问题）。
        from . import batch
        b = store.create_batch(sched.urls, label=f"[定时] {sched.label}")
        store.update_schedule(
            schedule_id,
            last_run_at=started,
            last_status="running",
            last_message="正在执行…（可在「批量下载 · 历史批次」看进度）",
            last_batch=b.batch_id,
            run_count=(sched.run_count or 0) + 1,
        )

        # 复用批量管线：定时任务本质就是"自动提交的一批链接"
        await batch._run_batch(b.batch_id)

        cur = store.get_batch(b.batch_id)
        counts = (cur.public(with_items=False).get("counts") if cur else {}) or {}
        ok, bad = counts.get("done", 0), counts.get("failed", 0)
        store.update_schedule(
            schedule_id,
            last_status="done" if ok else "failed",
            last_message=(f"完成 {ok} 条" + (f"，失败 {bad} 条" if bad else "")
                          + f"（批次 {b.batch_id}）"),
        )
        log.info("schedule %s finished: %s ok, %s failed", schedule_id, ok, bad)
    except asyncio.CancelledError:
        # 进程正在退出：把状态落成"已中断"，避免重启后永远停在"正在执行"
        store.update_schedule(schedule_id, last_status="failed",
                              last_message="服务已停止，本次执行被中断")
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("schedule %s failed", schedule_id)
        store.update_schedule(schedule_id, last_status=FAILED,
                              last_message=f"执行出错：{exc.__class__.__name__}")
    finally:
        _inflight.discard(schedule_id)
        # 执行完立刻重算下次时间：触发时算的"下次"用于防重复执行，
        # 但那时并不知道本次要跑多久；跑完后按"当前时刻"重算，语义才对。
        s = store.get_schedule(schedule_id)
        if s is not None and s.enabled:
            store.update_schedule(schedule_id, next_run_at=compute_next(s))


def reconcile_stale_runs() -> int:
    """启动时收拾"上次进程死在半路"的计划。

    进程被强杀时 ``_fire`` 的 finally 不会执行，``last_status`` 就会永久停在
    ``running``，界面上看起来像"卡住了"。启动时把它们落成"已中断"并重算下次时间。
    """
    fixed = 0
    for pub in store.list_schedules():
        if pub.get("last_status") != "running":
            continue
        sched = store.get_schedule(pub["schedule_id"])
        if sched is None:
            continue
        store.update_schedule(
            pub["schedule_id"],
            last_status="failed",
            last_message="上次执行未正常结束（服务曾重启或被中断）",
            next_run_at=compute_next(sched) if sched.enabled else None,
        )
        fixed += 1
    if fixed:
        log.info("reconciled %d stale schedule run(s)", fixed)
    return fixed


async def run_now(schedule_id: str) -> bool:
    """手动立即执行一次（不影响既定的下次时间）。"""
    sched = store.get_schedule(schedule_id)
    if sched is None or schedule_id in _inflight:
        return False
    asyncio.create_task(_fire(schedule_id))
    return True
