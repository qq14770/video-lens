/* ==========================================================================
   Video Lens — 批量下载 & 定时任务
   --------------------------------------------------------------------------
   独立性：本文件不碰 app.js 的单条解析状态机（state.data / state.gen / 播放器），
   只接管「批量下载」与「定时任务」两个面板。两块面板与结果区互斥显示，
   因此单条解析的界面约束（只有标题 + 播放器 + 直链）完全不受影响。

   为什么批量要轮询而不是等一个长请求：
     一次批量可能跑几分钟到几十分钟（HLS 要逐条合并），长连接会被浏览器/代理掐断，
     且无法显示"第几条了"。所以沿用后端既有的「任务 + 轮询」范式。
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    tabs: $("mode-tabs"),
    tabBtns: Array.from(document.querySelectorAll(".vl-tab")),

    // 单条解析原有的三块视图（切换模式时要一起隐藏）
    singleViews: [$("empty-state"), $("loading-state"), $("error-state"), $("result-state")],

    batch: $("batch-state"),
    batchInput: $("batch-input"),
    batchLabel: $("batch-label"),
    batchCount: $("batch-count"),
    batchSubmit: $("batch-submit"),
    batchProgress: $("batch-progress"),
    batchProgressLabel: $("batch-progress-label"),
    batchProgressPct: $("batch-progress-pct"),
    batchProgressFill: $("batch-progress-fill"),
    batchItems: $("batch-items"),
    batchActs: $("batch-acts"),
    batchDownload: $("batch-download"),
    batchClear: $("batch-clear"),
    batchHistWrap: $("batch-hist-wrap"),
    batchHist: $("batch-hist"),

    schedule: $("schedule-state"),
    scheduleInput: $("schedule-input"),
    scheduleLabel: $("schedule-label"),
    scheduleMode: $("schedule-mode"),
    scheduleAt: $("schedule-at"),
    scheduleEvery: $("schedule-every"),
    scheduleUnit: $("schedule-unit"),
    scheduleAdd: $("schedule-add"),
    scheduleList: $("schedule-list"),
    scheduleEmpty: $("schedule-empty"),

    tplBatchItem: $("tpl-batch-item"),
    tplSchedule: $("tpl-schedule"),
    toast: $("toast"),
  };

  const state = {
    mode: "single",
    batchId: null,
    batchTimer: null,
    batchDone: false,
    pollGen: 0,          // 代际号：换批次后作废旧轮询
    schedTimer: null,    // 定时面板的自动刷新
  };

  const POLL_MS = 1500;
  const SCHED_POLL_MS = 5000;   // 定时面板刷新间隔：执行可能几十分钟，5s 足够且不吵

  /* ----------------------------------------------------------------------
     小工具
     ---------------------------------------------------------------------- */
  /** 轻提示。必须与 app.js 用同一套钩子（.is-show + data-type），
      否则这里弹的提示会因为没有匹配的 CSS 而完全不显示。 */
  function toast(msg, kind) {
    el.toast.textContent = msg;
    el.toast.dataset.type = kind || "ok";
    el.toast.classList.add("is-show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.toast.classList.remove("is-show"), 3200);
  }

  async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (_) { /* 非 JSON（如 zip 流）走兜底 */ }
    if (!resp.ok) {
      const err = new Error((body && (body.detail || body.message)) || ("HTTP " + resp.status));
      err.code = "http_" + resp.status;
      throw err;
    }
    return body;
  }

  function fmtSize(bytes) {
    const n = Number(bytes) || 0;
    if (n <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + units[i];
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function extractUrls(text) {
    const out = [];
    const seen = new Set();
    const re = /https?:\/\/[^\s"'<>()\[\]，。；]+/gi;
    let m;
    while ((m = re.exec(text || "")) !== null) {
      const url = m[0].replace(/[.,;:，。；、）)】》」'"]+$/, "");
      if (url && !seen.has(url)) { seen.add(url); out.push(url); }
    }
    return out;
  }

  /* ----------------------------------------------------------------------
     模式切换
     ---------------------------------------------------------------------- */
  function setMode(mode) {
    state.mode = mode;
    el.tabBtns.forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });

    // 单条解析的四个视图只在 single 模式下显示。
    // 这里用 !important 级别的隐藏是靠 hidden 属性 + CSS 全局复位实现的，
    // 不会与 app.js 的 show() 打架：切回 single 时由它按自己的状态恢复。
    el.singleViews.forEach((v) => { v.hidden = mode !== "single"; });

    el.batch.hidden = mode !== "batch";
    el.schedule.hidden = mode !== "schedule";

    if (mode === "single") {
      document.dispatchEvent(new CustomEvent("vl:mode-single"));
    }
    if (mode === "batch") {
      loadHistory();
      el.batchInput.focus();
    }
    if (mode === "schedule") {
      loadSchedules();
      el.scheduleInput.focus();
      startSchedRefresh();
    } else {
      stopSchedRefresh();
    }
  }

  /** 定时面板自动刷新。

      为什么必须刷新：一次定时执行可能几十分钟（HLS 逐条合并），而计划列表如果只在
      切换模式时读一次，用户会一直看到"尚未执行过"，以为定时没生效 —— 实际反馈过。
      执行中时用 5s 轮询，全部静止后退回到低频（避免无意义的常驻请求）。 */
  function startSchedRefresh() {
    stopSchedRefresh();
    const tick = async () => {
      if (state.mode !== "schedule") return;
      const busy = await loadSchedules();
      // 有正在执行的计划 → 继续快刷；否则 20s 一次即可
      state.schedTimer = setTimeout(tick, busy ? SCHED_POLL_MS : 20000);
    };
    state.schedTimer = setTimeout(tick, SCHED_POLL_MS);
  }

  function stopSchedRefresh() {
    clearTimeout(state.schedTimer);
    state.schedTimer = null;
  }

  /* ======================================================================
     一、批量下载
     ====================================================================== */

  function updateBatchCount() {
    const n = extractUrls(el.batchInput.value).length;
    el.batchCount.textContent = `识别到 ${n} 条`;
    el.batchCount.dataset.ok = n > 0 ? "1" : "0";
  }

  async function submitBatch() {
    const urls = extractUrls(el.batchInput.value);
    if (!urls.length) {
      toast("没有识别到链接：请粘贴 http/https 开头的视频链接", "err");
      el.batchInput.focus();
      return;
    }

    el.batchSubmit.disabled = true;
    el.batchSubmit.textContent = "提交中…";
    try {
      const res = await api("/api/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: el.batchInput.value, label: el.batchLabel.value.trim() }),
      });
      state.batchId = res.batch_id;
      state.batchDone = false;
      renderBatchItems([]);
      el.batchActs.hidden = true;
      el.batchProgress.hidden = false;
      setProgress(0, `已提交 ${res.total} 条，正在解析…`);
      toast(`已提交 ${res.total} 条链接` + (res.rejected ? `（${res.rejected} 条不可用）` : ""),
            res.rejected ? "warn" : "ok");
      pollBatch(++state.pollGen);
    } catch (err) {
      toast(err.message || "提交失败", "err");
    } finally {
      el.batchSubmit.disabled = false;
      el.batchSubmit.textContent = "开始批量下载";
    }
  }

  function setProgress(ratio, label) {
    const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    el.batchProgressFill.style.width = pct + "%";
    el.batchProgressPct.textContent = pct + "%";
    if (label) el.batchProgressLabel.textContent = label;
  }

  function pollBatch(gen) {
    clearTimeout(state.batchTimer);
    const tick = async () => {
      if (gen !== state.pollGen || !state.batchId) return;
      let data;
      try {
        data = await api("/api/batch/" + encodeURIComponent(state.batchId));
      } catch (err) {
        // 404 = 批次被后端清理（重启/超 TTL）：如实告知，不无限重试
        if (err.code === "http_404") {
          setProgress(0, "批次已过期，请重新提交");
          toast("该批次已过期（服务重启或超时），请重新提交", "err");
          return;
        }
        state.batchTimer = setTimeout(tick, POLL_MS * 2);
        return;
      }
      if (gen !== state.pollGen) return;

      renderBatchItems(data.items || []);
      const settled = (data.counts.done || 0) + (data.counts.failed || 0);
      const label = data.pack_state === "packing"
        ? "正在打包 zip…"
        : `已完成 ${settled}/${data.total}（成功 ${data.counts.done || 0}` +
          (data.counts.failed ? `，失败 ${data.counts.failed}` : "") + "）";
      setProgress(data.progress || 0, label);

      if (data.finished) {
        state.batchDone = true;
        if (data.pack_state === "packed") {
          el.batchActs.hidden = false;
          setProgress(1, label + " · 打包完成，可以下载了");
          toast("打包完成，点「下载 zip」取走", "ok");
        } else {
          // empty：没有可打包内容（全失败）—— 给出后端说明的原因
          toast(data.pack_error || "没有可打包的内容", "err");
        }
        loadHistory();
        return;
      }
      state.batchTimer = setTimeout(tick, POLL_MS);
    };
    state.batchTimer = setTimeout(tick, 400);
  }

  function renderBatchItems(items) {
    el.batchItems.textContent = "";
    items.forEach((it) => {
      const node = el.tplBatchItem.content.cloneNode(true);
      const li = node.querySelector(".vl-item");
      li.dataset.status = it.status;
      li.querySelector(".vl-item__title").textContent = it.title || it.url;
      li.querySelector(".vl-item__url").textContent = it.url;
      const errEl = li.querySelector(".vl-item__err");
      if (it.error) { errEl.textContent = it.error; } else { errEl.remove(); }

      // 体积：已完成 → 实际值；进行中 → "已写 / 约总计"；等待中 → "约总计"
      const bits = [];
      if (it.status_text) bits.push(it.status_text);
      if (it.status === "running" && it.progress != null) {
        bits.push(it.progress + "%");
        if (it.size_bytes && it.size_estimate_bytes) {
          bits.push(`${fmtSize(it.size_bytes)} / 约 ${fmtSize(it.size_estimate_bytes)}`);
        } else if (it.size_bytes) {
          bits.push(fmtSize(it.size_bytes));
        }
      } else if (it.size_bytes) {
        bits.push(fmtSize(it.size_bytes));                      // 已完成的实际体积
      } else if (it.size_estimate_bytes) {
        bits.push("约 " + fmtSize(it.size_estimate_bytes));      // 还没跑完的预估值
      }
      li.querySelector(".vl-item__meta").textContent = bits.join(" · ");

      // 该条目自己的进度条（只在合并中显示），与顶部整批进度条互补：
      // 整批条看不出"卡在哪一条"，逐条条能。
      if (it.status === "running" && it.progress != null) {
        const bar = document.createElement("div");
        bar.className = "vl-item__prog";
        const fill = document.createElement("i");
        fill.style.width = Math.max(0, Math.min(100, it.progress)) + "%";
        bar.appendChild(fill);
        li.querySelector(".vl-item__main").appendChild(bar);
      }

      el.batchItems.appendChild(node);
    });
  }

  function downloadBatch() {
    if (!state.batchId) return;
    // 用 <a download> 触发，避免 fetch 把大 zip 读进内存
    const a = document.createElement("a");
    a.href = "/api/batch/" + encodeURIComponent(state.batchId) + "/file";
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("已开始下载 zip", "ok");
  }

  async function loadHistory() {
    try {
      const res = await api("/api/batches");
      const list = res.batches || [];
      if (!list.length) { el.batchHistWrap.hidden = true; return; }
      el.batchHistWrap.hidden = false;
      el.batchHist.textContent = "";
      list.forEach((b) => {
        const li = document.createElement("li");
        li.className = "vl-hist__row";

        const label = document.createElement("span");
        label.className = "vl-hist__label";
        label.textContent = b.label || b.batch_id;

        const meta = document.createElement("span");
        meta.className = "vl-hist__meta";
        meta.textContent = `${fmtTime(b.created_at)} · 成功 ${b.counts.done || 0}` +
          (b.counts.failed ? ` / 失败 ${b.counts.failed}` : "") +
          (b.finished && b.pack_state === "packed" ? " · 可下载" : "");

        const open = document.createElement("button");
        open.type = "button";
        open.className = "vl-btn vl-btn--mini";
        open.textContent = b.finished && b.pack_state === "packed" ? "下载" : "查看";
        open.addEventListener("click", () => openHistory(b));

        li.appendChild(label);
        li.appendChild(meta);
        li.appendChild(open);
        el.batchHist.appendChild(li);
      });
    } catch (_) {
      el.batchHistWrap.hidden = true;
    }
  }

  async function openHistory(b) {
    state.batchId = b.batch_id;
    state.batchDone = !!b.finished;
    state.pollGen += 1;                    // 作废旧轮询
    clearTimeout(state.batchTimer);
    el.batchProgress.hidden = false;
    const settled = (b.counts.done || 0) + (b.counts.failed || 0);
    setProgress(b.progress || 0,
      `历史批次：成功 ${b.counts.done || 0}` + (b.counts.failed ? `，失败 ${b.counts.failed}` : ""));
    el.batchActs.hidden = !(b.finished && b.pack_state === "packed");

    try {
      const data = await api("/api/batch/" + encodeURIComponent(b.batch_id));
      renderBatchItems(data.items || []);
      if (!data.finished) pollBatch(++state.pollGen);
    } catch (err) {
      toast(err.message || "无法读取该批次", "err");
    }
  }

  /* ======================================================================
     二、定时任务
     ====================================================================== */

  function syncModeInputs() {
    const interval = el.scheduleMode.value === "interval";
    el.scheduleAt.hidden = interval;
    el.scheduleEvery.hidden = !interval;
    el.scheduleUnit.hidden = !interval;
  }

  async function addSchedule() {
    const urls = extractUrls(el.scheduleInput.value);
    if (!urls.length) {
      toast("请先填入要定时处理的链接", "err");
      el.scheduleInput.focus();
      return;
    }
    el.scheduleAdd.disabled = true;
    el.scheduleAdd.textContent = "添加中…";
    try {
      await api("/api/schedules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: el.scheduleInput.value,
          label: el.scheduleLabel.value.trim(),
          mode: el.scheduleMode.value,
          at: el.scheduleAt.value || "22:00",
          every_minutes: Number(el.scheduleEvery.value) || 60,
        }),
      });
      el.scheduleInput.value = "";
      el.scheduleLabel.value = "";
      toast("已添加定时计划", "ok");
      loadSchedules();
    } catch (err) {
      toast(err.message || "添加失败", "err");
    } finally {
      el.scheduleAdd.disabled = false;
      el.scheduleAdd.textContent = "添加计划";
    }
  }

  async function loadSchedules() {
    let res;
    try {
      res = await api("/api/schedules");
    } catch (_) {
      return false;
    }
    const list = res.schedules || [];
    let anyRunning = false;
    el.scheduleList.textContent = "";
    el.scheduleEmpty.hidden = list.length > 0;
    if (!res.enabled) {
      el.scheduleEmpty.hidden = false;
      el.scheduleEmpty.textContent = "定时调度已在服务端关闭（VX_SCHEDULE_ENABLED=false）。";
    }

    list.forEach((s) => {
      if (s.last_status === "running") anyRunning = true;

      const node = el.tplSchedule.content.cloneNode(true);
      const li = node.querySelector(".vl-sched");
      li.dataset.enabled = s.enabled ? "1" : "0";
      li.dataset.running = s.last_status === "running" ? "1" : "0";

      li.querySelector(".vl-sched__label").textContent = s.label || s.schedule_id;

      // 「下次」与「正在执行」要分开说清楚：
      // 若正在跑，只说"下次 明天 15:10"会让人以为今天没跑（实际反馈过这个误解）。
      const when = s.mode === "interval" ? `每隔 ${s.every_minutes} 分钟` : `每天 ${s.at}`;
      const running = s.last_status === "running";
      li.querySelector(".vl-sched__when").textContent = !s.enabled
        ? `${when}　（已暂停）`
        : running
          ? `${when}　● 正在执行中…`
          : `${when}　下次：${fmtTime(s.next_run_at)}`;

      li.querySelector(".vl-sched__urls").textContent =
        `${s.urls.length} 条链接：` + s.urls.map((u) => u.slice(0, 48)).join("　");

      const last = li.querySelector(".vl-sched__last");
      if (running) {
        last.textContent = `上次执行 ${fmtTime(s.last_run_at)}：${s.last_message || "正在执行…"}`;
      } else if (s.last_run_at) {
        last.textContent = `上次执行 ${fmtTime(s.last_run_at)}：${s.last_message || s.last_status_text}`;
      } else {
        last.textContent = `尚未执行过　首次触发：${fmtTime(s.next_run_at)}`;
      }

      li.querySelector(".vl-sched__run").addEventListener("click", async (ev) => {
        ev.target.disabled = true;
        try {
          await api(`/api/schedules/${encodeURIComponent(s.schedule_id)}/run`, { method: "POST" });
          toast("已开始执行，下方会显示进度", "ok");
          loadSchedules();
          startSchedRefresh();
        } catch (err) {
          toast(err.message || "执行失败", "err");
        } finally {
          ev.target.disabled = false;
        }
      });

      const toggle = li.querySelector(".vl-sched__toggle");
      toggle.textContent = s.enabled ? "暂停" : "启用";
      toggle.addEventListener("click", async () => {
        try {
          await api(`/api/schedules/${encodeURIComponent(s.schedule_id)}`,
                    { method: "PATCH", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ enabled: !s.enabled }) });
          toast(s.enabled ? "已暂停该计划" : "已启用该计划", "ok");
          loadSchedules();
        } catch (err) {
          toast(err.message || "操作失败", "err");
        }
      });

      li.querySelector(".vl-sched__del").addEventListener("click", async () => {
        if (!window.confirm(`确定删除计划「${s.label || s.schedule_id}」？`)) return;
        try {
          await api(`/api/schedules/${encodeURIComponent(s.schedule_id)}`, { method: "DELETE" });
          toast("已删除", "ok");
          loadSchedules();
        } catch (err) {
          toast(err.message || "删除失败", "err");
        }
      });

      el.scheduleList.appendChild(node);
    });

    return anyRunning;
  }

  /* ======================================================================
     初始化
     ====================================================================== */
  function init() {
    el.tabBtns.forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

    el.batchInput.addEventListener("input", updateBatchCount);
    el.batchSubmit.addEventListener("click", submitBatch);
    el.batchDownload.addEventListener("click", downloadBatch);
    el.batchClear.addEventListener("click", () => {
      state.pollGen += 1;
      clearTimeout(state.batchTimer);
      state.batchId = null;
      state.batchDone = false;
      el.batchInput.value = "";
      el.batchLabel.value = "";
      el.batchItems.textContent = "";
      el.batchProgress.hidden = true;
      el.batchActs.hidden = true;
      updateBatchCount();
    });

    el.scheduleMode.addEventListener("change", syncModeInputs);
    el.scheduleAdd.addEventListener("click", addSchedule);
    syncModeInputs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
