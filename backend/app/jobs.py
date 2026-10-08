"""异步解析任务表（纯内存、LRU + TTL）。

为什么用「任务 + 轮询」而不是同步长请求：
  - 部分站点解析要 10~30s，同步请求容易被浏览器/代理层超时截断，且前端无法显示进度；
  - 任务化后 ``POST /api/extract`` 立即返回 ``job_id``，前端每 700ms 轮询一次状态，
    体验上就是「正在识别站点 → 正在解析清晰度 → 完成」。

并发与容量：``JOB_CAPACITY`` 上限 + 完成即过期，保证长期运行不会泄漏内存。
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import config

# 任务状态机
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

_STEPS = {
    QUEUED: "已排队，等待解析…",
    RUNNING: "正在解析视频信息…",
    DONE: "解析完成",
    FAILED: "解析失败",
}

#: HLS 合并任务专用文案：状态机复用，但字面上不能说「解析」
#: （前端合并轮询会把 step 显示为进度提示 / 按钮 title）
STEPS_REMUX = {
    QUEUED: "已排队，等待合并…",
    RUNNING: "正在合并 HLS 分片…",
    DONE: "合并完成",
    FAILED: "合并失败",
}


@dataclass
class Job:
    job_id: str
    url: str
    status: str = QUEUED
    step: str = _STEPS[QUEUED]
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    #: 0-99 的百分比；``None`` 表示"无法可靠估算进度"（例如拿不到清单总时长）
    progress: int | None = None
    #: 已写入字节 / 预估总字节 —— 前端据此显示「12.3 MB / 约 45.6 MB」
    written_bytes: int = 0
    estimate_bytes: int | None = None

    def public(self) -> dict[str, Any]:
        """前端只需要的字段（保持返回体极小，方便轮询）。"""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "step": self.step,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "result": self.result if self.status == DONE else None,
            "error": self.error if self.status == FAILED else None,
            # 进度三件套：百分比 + 已写字节 + 预估字节。
            # DONE 时强制 100%，避免出现"结果已出但进度条停在 99%"的矛盾画面。
            "progress": 100 if self.status == DONE else self.progress,
            "written_bytes": self.written_bytes or None,
            "estimate_bytes": self.estimate_bytes,
        }


class JobTable:
    """带 TTL 淘汰的 LRU 任务表；所有方法都是同步的（内部用锁语义由 asyncio 单线程保证）。"""

    def __init__(self, capacity: int | None = None, ttl: int | None = None) -> None:
        self._items: "OrderedDict[str, Job]" = OrderedDict()
        self._capacity = capacity or config.JOB_CAPACITY
        self._ttl = ttl or config.JOB_TTL

    # ---- 生命周期 ----
    def create(self, url: str) -> Job:
        self._evict()
        job = Job(job_id=uuid.uuid4().hex[:12], url=url)
        self._items[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        job = self._items.get(job_id)
        if job:
            self._items.move_to_end(job_id)  # 命中即视为最近使用
        return job

    def update(self, job_id: str, **changes: Any) -> None:
        job = self._items.get(job_id)
        if not job:
            return
        for key, value in changes.items():
            setattr(job, key, value)
        self._items.move_to_end(job_id)

    # ---- 内部 ----
    def _evict(self) -> None:
        """清理过期任务；仍超容量时按插入顺序（最久未用）淘汰。"""
        now = time.time()
        expired = [
            jid
            for jid, job in self._items.items()
            if job.finished_at and now - job.finished_at > self._ttl
        ]
        for jid in expired:
            self._items.pop(jid, None)
        while len(self._items) >= self._capacity:
            self._items.popitem(last=False)

    def fail(self, job_id: str, code: str, message: str,
             step: str | None = None) -> None:
        """外部看门狗用：把仍在运行但已超时的任务判失败（线程本身会自行退出）。"""
        job = self._items.get(job_id)
        if job and job.status in (QUEUED, RUNNING):
            self.update(job_id, status=FAILED, step=step or _STEPS[FAILED],
                        error={"code": code, "message": message},
                        finished_at=time.time())

    def run(self, job_id: str,
            work: Callable[[str, Callable[[str], None]], dict[str, Any]],
            steps: dict[str, str] | None = None) -> None:
        """在线程里执行阻塞的 ``work(url, progress)``，并负责状态机流转。

        ``steps`` 可换成 :data:`STEPS_REMUX`：合并任务复用的是同一套状态机，
        但默认文案里的「解析」对它并不成立。
        """
        job = self._items.get(job_id)
        if job is None:
            return
        steps = steps or _STEPS
        self.update(job_id, status=RUNNING, step=steps[RUNNING])

        def progress(step: str) -> None:
            self.update(job_id, step=step)

        try:
            result = work(job.url, progress)
            cur = self._items.get(job_id)
            # 看门狗可能已把超时任务判失败 —— 此时丢弃迟到的结果，避免状态回跳
            if cur is None or cur.status != RUNNING:
                return
            self.update(job_id, status=DONE, step=steps[DONE], result=result,
                        finished_at=time.time())
        except Exception as exc:  # noqa: BLE001 —— 需要把所有异常转成用户可读文案
            from .extractor import ExtractError

            friendly = (str(exc) if isinstance(exc, ExtractError)
                        else "解析失败：目标站点可能改版或需要登录 Cookie")
            self.update(job_id, status=FAILED, step=steps[FAILED],
                        error={"code": getattr(exc, "code", "extract_failed"),
                               "message": friendly},
                        finished_at=time.time())


jobs = JobTable()
