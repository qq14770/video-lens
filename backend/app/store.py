"""批量任务与定时计划的持久化存储。

**为什么需要持久化**

批量下载与定时下载都是"提交后就不该盯着屏幕"的场景：用户可能排一批链接就关掉页面，
或定一个每天 22:00 自动跑的计划。这些状态必须活过进程重启，否则"定时"毫无意义。
故本模块把状态写到磁盘上的一个 JSON 文件，而不是放内存。

**设计要点**

1. **原子写入**：先写临时文件再 ``os.replace``。直接覆写时若进程被杀，会留下半截 JSON
   导致下次启动整个文件读不出来 —— 那意味着所有计划一起丢失。
2. **单文件 + 内存镜像**：数据量很小（几十条计划），全部读入内存，读操作零 IO；
   写操作加锁 + 原子替换。不用 sqlite 是为了零依赖、可直接用文本编辑器查看/备份。
3. **结构版本号**：``schema`` 字段便于将来迁移；读到不认识的结构就当作空库并备份旧文件，
   绝不让格式变更把用户的计划静默吃掉。
4. **不存敏感信息**：只存 URL、时间、状态这些必要字段。直链（带站点签名）不落盘 ——
   它们本来就有时效，存下来既没用又多一处泄露面。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import config

log = logging.getLogger("video-lens.store")

SCHEMA = 1

#: 批量条目 / 定时计划的运行状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

_STATUS_TEXT = {
    PENDING: "等待中",
    RUNNING: "进行中",
    DONE: "已完成",
    FAILED: "失败",
    SKIPPED: "已跳过",
}


def _now() -> float:
    return time.time()


@dataclass
class BatchItem:
    """批量任务里的一条链接。"""

    item_id: str
    url: str
    status: str = PENDING
    title: str = ""
    provider: str = ""
    # 提取到的默认源（只留必要字段，直链本身不落盘，见模块 docstring）
    duration: int | None = None
    source_count: int = 0
    is_hls: bool = False
    size_bytes: int = 0
    #: 体积预估（合并前按码率×时长算），用于在结果出来前就告诉用户大概多大
    size_estimate_bytes: int = 0
    #: 合并进度 0-99（HLS 条目）；``None`` = 无法可靠估算
    progress: int | None = None
    error: str = ""
    remux_job: str = ""            # 合并任务 id（HLS 条目合并时）
    updated_at: float = field(default_factory=_now)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["status_text"] = _STATUS_TEXT.get(self.status, self.status)
        # 展示用体积：已完成取实际值，进行中/等待中退回预估值
        d["display_bytes"] = self.size_bytes or self.size_estimate_bytes or 0
        return d


@dataclass
class Batch:
    """一次批量提交。"""

    batch_id: str
    items: list[BatchItem] = field(default_factory=list)
    created_at: float = field(default_factory=_now)
    label: str = ""
    #: 打包阶段：``idle`` → ``packing`` → ``packed``（无内容可打包时 ``empty``）。
    #: 单独用一个状态位而不是只看条目状态 —— 条目全 DONE 之后还要打包，这中间有个
    #: 窗口期：若此时就告诉前端"完成了"，用户点下载会拿到 409/410。
    pack_state: str = "idle"
    pack_error: str = ""

    def public(self, with_items: bool = True) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for it in self.items:
            counts[it.status] = counts.get(it.status, 0) + 1
        done = counts.get(DONE, 0)
        failed = counts.get(FAILED, 0)
        total = len(self.items)
        items_settled = total > 0 and done + failed >= total
        return {
            "batch_id": self.batch_id,
            "label": self.label,
            "created_at": self.created_at,
            "total": total,
            "counts": counts,
            # 真正"可以取件"的判定：条目全部落定 **且** 打包阶段结束
            "finished": items_settled and self.pack_state in ("packed", "empty"),
            "items_settled": items_settled,
            "pack_state": self.pack_state,
            "pack_error": self.pack_error,
            "progress": round((done + failed) / total, 3) if total else 0.0,
            "items": [i.public() for i in self.items] if with_items else [],
        }


@dataclass
class Schedule:
    """一个定时计划：到点自动提取（必要时合并）指定链接。

    时间表达刻意保持简单可预测：``mode`` 取 ``daily``（每天某时刻）或 ``interval``
    （每 N 分钟）。不引入 cron 表达式 —— 那需要额外依赖，且对"每天备份一次"这种
    真实需求属于过度设计；真正需要复杂排程时用户可以用系统计划任务调本服务的 API。
    """

    schedule_id: str
    urls: list[str] = field(default_factory=list)
    mode: str = "daily"                 # daily | interval
    at: str = "22:00"                   # daily: "HH:MM"
    every_minutes: int = 60             # interval: 间隔分钟
    enabled: bool = True
    label: str = ""
    # 运行记录
    last_run_at: float | None = None
    next_run_at: float | None = None
    last_status: str = ""
    last_message: str = ""
    #: 本次（或上次）执行产生的批次 id —— 界面据此给出"去看进度"的入口
    last_batch: str = ""
    run_count: int = 0
    created_at: float = field(default_factory=_now)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["last_status_text"] = _STATUS_TEXT.get(self.last_status, self.last_status or "—")
        return d


class Store:
    """JSON 文件 + 内存镜像的持久化容器（批量与计划共用）。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or config.STORE_FILE
        self._lock = threading.RLock()
        self._batches: dict[str, Batch] = {}
        self._schedules: dict[str, Schedule] = {}
        self._loaded = False

    # ---- 载入 / 落盘 ----

    def load(self) -> None:
        """启动时调用一次。文件不存在属正常（首次运行）。"""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            raw = self._read()
            if raw is None:
                return
            self._batches = {}
            for b in raw.get("batches") or []:
                try:
                    items = [BatchItem(**i) for i in b.get("items") or []]
                    self._batches[b["batch_id"]] = Batch(
                        batch_id=b["batch_id"], items=items,
                        created_at=b.get("created_at") or _now(),
                        label=b.get("label", ""))
                except (TypeError, KeyError) as exc:
                    log.warning("skip malformed batch: %s", exc)
            self._schedules = {}
            for s in raw.get("schedules") or []:
                try:
                    self._schedules[s["schedule_id"]] = Schedule(**s)
                except TypeError as exc:
                    log.warning("skip malformed schedule: %s", exc)
            log.info("store loaded: %d batch(es), %d schedule(s)",
                     len(self._batches), len(self._schedules))

    def _read(self) -> dict[str, Any] | None:
        if not os.path.isfile(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # 文件损坏：备份一份再当空库启动，绝不静默丢数据
            log.error("store unreadable (%s), backing up and starting empty", exc)
            self._backup_broken()
            return None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            log.warning("store schema mismatch (got %r, want %r), backing up",
                        (raw or {}).get("schema") if isinstance(raw, dict) else None, SCHEMA)
            self._backup_broken()
            return None
        return raw

    def _backup_broken(self) -> None:
        try:
            if os.path.isfile(self._path):
                shutil.copy2(self._path, self._path + ".broken")
        except OSError as exc:  # pragma: no cover
            log.warning("cannot back up broken store: %s", exc)

    def save(self) -> None:
        """原子落盘：写临时文件 → fsync → os.replace（同目录，保证原子）。"""
        with self._lock:
            payload = {
                "schema": SCHEMA,
                "saved_at": _now(),
                "batches": [
                    {"batch_id": b.batch_id, "label": b.label, "created_at": b.created_at,
                     "items": [asdict(i) for i in b.items]}
                    for b in self._batches.values()
                ],
                "schedules": [asdict(s) for s in self._schedules.values()],
            }
            directory = os.path.dirname(os.path.abspath(self._path)) or "."
            try:
                os.makedirs(directory, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=directory, prefix=".store-", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, ensure_ascii=False, indent=1)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, self._path)          # 原子替换
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                log.error("store save failed: %s", exc)

    # ---- 批量 ----

    def create_batch(self, urls: list[str], label: str = "",
                     rejected: list[tuple[str, str]] | None = None) -> Batch:
        """建一批。``rejected`` 里的链接直接标失败并记原因（不参与后续处理）。"""
        with self._lock:
            items = [BatchItem(item_id=uuid.uuid4().hex[:10], url=u) for u in urls]
            for bad_url, why in (rejected or []):
                items.append(BatchItem(item_id=uuid.uuid4().hex[:10], url=bad_url,
                                       status=FAILED, error=f"链接不可用：{why}"))
            batch = Batch(
                batch_id=uuid.uuid4().hex[:12],
                label=label or f"{len(items)} 条链接",
                items=items,
            )
            self._batches[batch.batch_id] = batch
            self._trim_batches()
        self.save()
        return batch

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._batches.values(), key=lambda b: b.created_at, reverse=True)
            return [b.public(with_items=False) for b in ordered[:limit]]

    def update_batch(self, batch_id: str, **changes: Any) -> None:
        """更新批次级字段（如打包阶段）。"""
        with self._lock:
            batch = self._batches.get(batch_id)
            if not batch:
                return
            for k, v in changes.items():
                setattr(batch, k, v)
        self.save()

    def update_item(self, batch_id: str, item_id: str, **changes: Any) -> None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if not batch:
                return
            for it in batch.items:
                if it.item_id == item_id:
                    for k, v in changes.items():
                        setattr(it, k, v)
                    it.updated_at = _now()
                    break
        self.save()

    def delete_batch(self, batch_id: str) -> bool:
        with self._lock:
            gone = self._batches.pop(batch_id, None) is not None
        if gone:
            self.save()
        return gone

    def _trim_batches(self, keep: int = 30) -> None:
        """只保留最近若干批，避免长期运行把文件撑大（调用方已持锁）。"""
        if len(self._batches) <= keep:
            return
        ordered = sorted(self._batches.values(), key=lambda b: b.created_at)
        for b in ordered[: len(self._batches) - keep]:
            self._batches.pop(b.batch_id, None)

    # ---- 定时计划 ----

    def add_schedule(self, sched: Schedule) -> Schedule:
        with self._lock:
            self._schedules[sched.schedule_id] = sched
        self.save()
        return sched

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            return self._schedules.get(schedule_id)

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.public() for s in sorted(
                self._schedules.values(), key=lambda s: s.created_at)]

    def update_schedule(self, schedule_id: str, **changes: Any) -> Schedule | None:
        with self._lock:
            sched = self._schedules.get(schedule_id)
            if sched is None:
                return None
            for k, v in changes.items():
                setattr(sched, k, v)
        self.save()
        return sched

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            gone = self._schedules.pop(schedule_id, None) is not None
        if gone:
            self.save()
        return gone


store = Store()
