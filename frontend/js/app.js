/* ==========================================================================
   Video Lens — 前端主控脚本
   --------------------------------------------------------------------------
   职责（保持"薄"，所有过滤/解析都在后端完成）：

     1. 提交链接 → 轮询任务 → 渲染结果（状态机：empty / loading / error / result）
     2. 播放源切换：mp4 系走原生 <video>，m3u8 系走 hls.js（Safari 原生可播则不加载）
     3. 直链区：复制、导出 JSON、新窗口打开
     4. 键盘效率：Ctrl/Cmd+V 全局粘贴即取、Enter 提交、Esc 关闭浮层、Space 播放

   约定：
     - 不引入任何框架与第三方 UI 资源；DOM 一律用 getElementById / template.cloneNode
     - 所有来自后端的文本都通过 textContent 写入（绝不 innerHTML 拼接），避免 XSS
   ========================================================================== */

(function () {
  "use strict";

  /* ----------------------------------------------------------------------
     0. DOM 引用
     ---------------------------------------------------------------------- */
  const $ = (id) => document.getElementById(id);

  const el = {
    form: $("extract-form"),
    url: $("url-input"),
    submit: $("submit-btn"),
    paste: $("paste-btn"),
    health: $("health"),
    healthLed: $("health").querySelector(".vl__status-led"),
    healthText: $("health-text"),

    empty: $("empty-state"),
    siteTags: $("site-tags"),

    loading: $("loading-state"),
    loadFill: $("loading-bar-fill"),
    loadStep: $("loading-step"),
    loadUrl: $("loading-url"),
    cancel: $("cancel-btn"),

    error: $("error-state"),
    errCode: $("error-code"),
    errMsg: $("error-msg"),
    errTips: $("error-tips"),
    retry: $("retry-btn"),
    reset: $("reset-btn"),

    result: $("result-state"),
    title: $("video-title"),
    fromDot: $("from-dot"),
    fromName: $("from-name"),
    duration: $("video-duration"),
    adBadge: $("ad-badge"),

    player: $("player-shell"),
    video: $("video"),
    poster: $("poster-mask"),
    posterImg: $("poster-img"),
    bigPlay: $("big-play"),
    note: $("player-note"),

    quality: $("quality-menu"),
    qualityBtn: $("quality-btn"),
    qualityCur: $("quality-cur"),
    qualityList: $("quality-list"),

    linksList: $("links-list"),
    copyAll: $("copy-all-btn"),
    exportJson: $("export-btn"),

    tplLink: $("tpl-link-item"),
    tplQuality: $("tpl-quality-item"),
    toast: $("toast"),
    footVer: $("foot-ver"),
  };

  /* ----------------------------------------------------------------------
     1. 应用状态
     ---------------------------------------------------------------------- */
  const state = {
    data: null,        // 后端 result 对象
    current: -1,       // 当前播放源下标
    jobId: null,       // 进行中的任务 id
    gen: 0,            // 提取代际号：每次 startExtract 自增，旧请求的迟到响应凭它作废
    pollTimer: null,
    pollStart: 0,
    hls: null,         // hls.js 实例
    lastUrl: "",       // 重试用
    searching: false,
    ffmpeg: false,     // 后端是否能合并 HLS（/api/health 的 ffmpeg_available）
    remuxGen: 0,       // 直链区代际号：重新渲染后作废旧的合并轮询
  };

  const POLL_MS = 700;
  const POLL_TIMEOUT_MS = 90000;   // 前端侧兜底超时（后端单任务默认 45s）

  const SITES = ["哔哩哔哩", "抖音", "YouTube", "X / Twitter", "小红书", "微博",
                 "TikTok", "Vimeo", "腾讯视频", "优酷", "爱奇艺", "芒果TV"];

  /* ----------------------------------------------------------------------
     2. 小工具
     ---------------------------------------------------------------------- */

  /** 统一走 fetch 的 JSON 请求；把 HTTP 错误翻译成 {code, message}。 */
  async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (_) { /* 非 JSON 响应，走下方兜底 */ }
    if (!resp.ok) {
      const message = (body && (body.detail || (body.error && body.error.message))) ||
                      `请求失败（HTTP ${resp.status}）`;
      const err = new Error(typeof message === "string" ? message : JSON.stringify(message));
      err.code = (body && (body.error && body.error.code)) || `http_${resp.status}`;
      throw err;
    }
    return body;
  }

  /** 秒 → "01:23:45" / "03:07"。 */
  function fmtDuration(sec) {
    if (!sec && sec !== 0) return "";
    const s = Math.round(sec);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    const p = (n) => String(n).padStart(2, "0");
    return h ? `${p(h)}:${p(m)}:${p(ss)}` : `${p(m)}:${p(ss)}`;
  }

  /** 安全文件名：去掉路径非法字符，附带清晰度，供「下载」按钮使用。 */
  function filenameFor(i, s, mp4) {
    // 标题里的中文括号补充说明（如「未命名视频（m3u8 直链不含标题信息）」）不进文件名
    const base = ((state.data && state.data.title) || "video")
      .replace(/（[^）]*）/g, "")
      .replace(/[\\/:*?"<>|\r\n]/g, "_").slice(0, 60).trim() || "video";
    let ext = s.ext || "mp4";
    if (s.is_hls) {
      // HLS 默认由后端合并成 MP4（清单本身不含画面，存盘也打不开）；
      // mp4 === false 表示刻意要原始清单（降级路径），mp4 省略时看后端有没有 ffmpeg。
      ext = (mp4 === undefined ? state.ffmpeg : mp4) ? "mp4" : "m3u8";
    }
    const q = (s.label || "").split("·")[0].trim().replace(/\s+/g, "") || ("src" + (i + 1));
    return `${base}_${q}.${ext}`;
  }

  /** 字节 → 可读体积。 */
  function fmtSize(bytes) {
    if (!bytes) return "";
    const units = ["B", "KB", "MB", "GB"];
    let n = bytes, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  }

  let toastTimer = null;
  /** 轻提示；type: ok | err | warn */
  function toast(msg, type) {
    el.toast.textContent = msg;
    el.toast.dataset.type = type || "ok";
    el.toast.classList.add("is-show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.toast.classList.remove("is-show"), 2400);
  }

  /** 复制文本：clipboard API 不可用（http 环境）时退回 execCommand。 */
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* 落到兜底分支 */ }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (_) {
      return false;
    }
  }

  /** 视图状态机：任一时刻只有一个视图可见。

      注意：整页还有「批量下载 / 定时任务」两个独立面板（panels.js 管）。
      切到那些模式时本文件的四个视图必须保持隐藏，否则会出现两套界面同屏；
      模式切回 single 时由 panels.js 派发 vl:mode-single 事件通知本文件重画。
      因此这里记录一个 current 名，切模式时用它恢复。 */
  let currentView = "empty";

  function show(name) {
    currentView = name;
    const map = { empty: el.empty, loading: el.loading, error: el.error, result: el.result };
    Object.entries(map).forEach(([key, node]) => {
      if (key === name) { node.hidden = false; }
      else { node.hidden = true; node.classList.remove("is-show"); }
    });
    document.body.dataset.view = name;
  }

  // 从其它模式切回单条解析：恢复本文件最后一次设定的视图
  document.addEventListener("vl:mode-single", () => show(currentView));

  /* ----------------------------------------------------------------------
     3. 提取流程
     ---------------------------------------------------------------------- */

  async function startExtract(rawUrl) {
    const url = (rawUrl || "").trim();
    if (!url) { el.url.focus(); return; }
    if (!/^(https?:\/\/|[\w.-]+\/|[\w.-]+\.[a-z]{2,})/i.test(url)) {
      show("error");
      renderError({ code: "bad_input", message: "看起来不是有效链接", tips:
        ["请粘贴完整分享链接，例如 https://b23.tv/xxxx", "微信/抖音分享文案里的中文说明可以先去掉"] });
      return;
    }

    state.lastUrl = url;
    state.gen += 1;               // 开启新的一代：此前在途的响应到回来时一律作废
    state.searching = true;
    setBusy(true);
    show("loading");
    el.loadUrl.textContent = url;
    el.loadUrl.title = url;
    progress(6, "已提交到本地解析服务…");

    stopHls();
    const gen = state.gen;
    try {
      const job = await api("/api/extract", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (gen !== state.gen) return;   // 提交响应回来时已开启新提取 → 丢弃
      state.jobId = job.job_id;
      state.pollStart = Date.now();
      poll();
    } catch (err) {
      if (gen !== state.gen) return;
      fail(err);
    }
  }

  /** 更新进度条与文案（纯视觉，不代表真实百分比）。 */
  function progress(pct, step) {
    el.loadFill.style.width = Math.min(100, pct) + "%";
    if (step) el.loadStep.textContent = step;
  }

  /** 轮询任务状态直到 done / failed / 超时。 */
  function poll() {
    clearTimeout(state.pollTimer);
    if (!state.searching) return;

    if (Date.now() - state.pollStart > POLL_TIMEOUT_MS) {
      fail({ code: "timeout", message: "解析超时：站点响应过慢或被拦截" });
      return;
    }

    const gen = state.gen;          // 本轮轮询所属的提取代际
    const jobId = state.jobId;
    state.pollTimer = setTimeout(async () => {
      if (!state.searching || gen !== state.gen) return;
      try {
        const job = await api("/api/jobs/" + encodeURIComponent(jobId));
        // await 之后必须重新核验代际：旧任务被新提取顶替时，它的迟到响应
        // （无论 done 还是 failed）不得再改动当前界面，否则错误面板会盖住新结果
        if (!state.searching || gen !== state.gen || jobId !== state.jobId) return;
        // 时间映射到 10~92%，让进度条持续有反馈但不假装精确
        const spent = Date.now() - state.pollStart;
        progress(10 + Math.min(82, (spent / 30000) * 82), job.step);

        if (job.status === "done") { render(job.result); setBusy(false); state.searching = false; return; }
        if (job.status === "failed") { fail(job.error); return; }
        poll();
      } catch (err) {
        if (!state.searching || gen !== state.gen || jobId !== state.jobId) return;
        // 任务表被重启/淘汰：视为可恢复错误，直接回到输入态
        if (String(err.code) === "http_404") { fail(err); } else { poll(); }
      }
    }, POLL_MS);
  }

  function setBusy(busy) {
    el.submit.disabled = busy;
    el.submit.textContent = busy ? "解析中" : "提取";
  }

  /* ----------------------------------------------------------------------
     3b. HLS 下载：先在后端合并成 MP4
     m3u8 只是「分片清单」，直接存盘是一份打不开的文本文件 —— 浏览器能预览
     是因为 hls.js 在逐片取流。所以下载必须走 /api/remux 让 ffmpeg 合并。
     本流程只改写那一个按钮的文案 + 轻提示，不新增任何面板（界面约束）。
     ---------------------------------------------------------------------- */

  const REMUX_POLL_MS = 1000;

  /** 按钮三态：idle（去合并）→ busy（合并中）→ ready（保存产物）。 */
  function setDlIdle(btn, s, i) {
    btn.dataset.mode = "idle";
    btn.removeAttribute("href");
    btn.removeAttribute("download");
    btn.removeAttribute("target");   // 合并链路上不做外链跳转
    const size = fmtSize(s && (s.size_bytes || s.size_estimate_bytes));
    btn.textContent = size ? `下载 MP4（${size}）` : "下载 MP4";
    btn.title = size
      ? `服务端合并为一个 MP4 · 预计约 ${size}`
      : "服务端把 HLS 合并成一个可直接播放的 MP4 文件";
    hideDlProgress(btn);
  }

  function setDlBusy(btn, text, pct) {
    btn.dataset.mode = "busy";
    btn.removeAttribute("href");
    btn.removeAttribute("download");
    btn.removeAttribute("target");   // 避免 busy 态被当成外链打开
    btn.textContent = pct != null ? `${text} ${pct}%` : text;
  }

  function setDlReady(btn, jobId, name, sizeBytes) {
    btn.dataset.mode = "ready";
    btn.href = "/api/remux/" + encodeURIComponent(jobId) + "/file";
    btn.setAttribute("download", name);
    btn.removeAttribute("target");   // 同源 attachment：留在本页触发下载，别开新标签
    btn.textContent = "保存 MP4" + (sizeBytes ? `（${fmtSize(sizeBytes)}）` : "");
    btn.title = sizeBytes ? `合并完成 · ${fmtSize(sizeBytes)}` : "点击保存";
    hideDlProgress(btn);
    toast("合并完成" + (sizeBytes ? " · " + fmtSize(sizeBytes) : "") + "，点击「保存 MP4」", "ok");
  }

  /* ---- 合并进度条：挂在直链行里，不新增任何面板（界面约束） ---- */

  /** 找到（或懒创建）该直链行内的进度条元素。 */
  function dlProgressEl(btn) {
    const row = btn.closest(".vl-link");
    if (!row) return null;
    let bar = row.querySelector(".vl-dlprog");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "vl-dlprog";
      bar.innerHTML = '<i class="vl-dlprog__fill"></i>'
                    + '<span class="vl-dlprog__text"></span>';
      // 插到直链行的最下方，占满整行，不挤压按钮
      const host = row.querySelector(".vl-link__row") || row;
      host.appendChild(bar);
    }
    bar.hidden = false;
    return bar;
  }

  function showDlProgress(btn, ratio, text, indeterminate) {
    const bar = dlProgressEl(btn);
    if (!bar) return;
    const fill = bar.querySelector(".vl-dlprog__fill");
    bar.dataset.indeterminate = indeterminate ? "1" : "0";
    if (indeterminate) {
      fill.style.width = "100%";
    } else {
      const pct = Math.max(0, Math.min(100, Math.round((ratio || 0) * 100)));
      fill.style.width = pct + "%";
    }
    bar.querySelector(".vl-dlprog__text").textContent = text || "";
  }

  function hideDlProgress(btn) {
    const row = btn.closest(".vl-link");
    const bar = row && row.querySelector(".vl-dlprog");
    if (bar) { bar.hidden = true; }
  }

  /** 发起合并任务，然后用这条按钮自己的轮询跟进（与提取轮询互不干扰）。 */
  async function startRemux(s, i, btn) {
    const qs = new URLSearchParams((s.play_url || "").split("?")[1] || "");
    const u = qs.get("u");
    if (!u) { toast("该直链的代理参数已失效，请重新提取", "err"); return; }
    const name = filenameFor(i, s, true);
    const query = new URLSearchParams({ u, t: qs.get("t") || "", filename: name });
    setDlBusy(btn, "提交中…");
    showDlProgress(btn, 0, "正在提交合并任务…", true);
    let job = null;
    try {
      job = await api("/api/remux?" + query.toString(), { method: "POST" });
    } catch (err) {
      setDlIdle(btn, s, i);
      toast(err.message || "无法发起合并", "err");
      return;
    }
    if (btn.dataset.mode !== "busy") return;      // 等待期间界面已重绘
    pollRemux(btn, job, name, s, i);
  }

  /** 合并进度轮询：以 remuxGen 作代际护栏，绝不影响提取流程的状态位。

      进度分母来自后端：ffmpeg 边下边写，`written_bytes` 是真实已写量，
      `estimate_bytes` 是合并前分析清单得出的预估总量，两者相除即百分比。
      拿不到预估时后端返回 progress=null，这里就画不确定态（滚动条），
      绝不编一个假百分比。 */
  function pollRemux(btn, job, name, s, i) {
    const gen = state.remuxGen;
    const jobId = job.job_id;
    const budget = (job.expected_ms || 900000) + 20000;   // 后端看门狗 + 网络余量
    const t0 = Date.now();
    const tick = async () => {
      if (gen !== state.remuxGen) return;                 // 直链区已重绘 → 作废
      if (Date.now() - t0 > budget) {
        setDlIdle(btn, s, i);
        toast("合并超时：源站取流过慢或已断开，可稍后重试", "err");
        return;
      }
      try {
        const st = await api("/api/jobs/" + encodeURIComponent(jobId));
        if (gen !== state.remuxGen) return;
        if (st.status === "done") {
          setDlReady(btn, jobId, name, st.result && st.result.size_bytes);
          return;
        }
        if (st.status === "failed") {
          setDlIdle(btn, s, i);
          toast((st.error && st.error.message) || "合并失败", "err");
          return;
        }
        renderRemuxProgress(btn, st, t0);
      } catch (err) {
        if (gen !== state.remuxGen) return;
        if (String(err.code) === "http_404") {
          setDlIdle(btn, s, i);
          toast("合并任务已过期，请重新点击下载", "err");
          return;
        }
      }
      setTimeout(tick, REMUX_POLL_MS);
    };
    setTimeout(tick, REMUX_POLL_MS);
  }

  /** 把任务状态画成进度条 + 按钮文案。

      文案优先级：能显示「已写 / 约总计」就显示（用户最关心还要多久、多大）；
      退而求其次显示预估总量；再不行才只报耗时。 */
  function renderRemuxProgress(btn, st, t0) {
    const pct = st.progress;
    const written = Number(st.written_bytes) || 0;
    const estimate = Number(st.estimate_bytes) || 0;
    const elapsed = fmtDuration((Date.now() - t0) / 1000);

    // 组装"已写 / 约总计"这段，缺哪部分就退到哪部分，但**尽量别丢掉体积信息**
    let detail;
    if (written && estimate) {
      detail = `${fmtSize(written)} / 约 ${fmtSize(estimate)} · 已用 ${elapsed}`;
    } else if (estimate) {
      detail = `约 ${fmtSize(estimate)} · 已用 ${elapsed}`;
    } else if (written) {
      detail = `已写入 ${fmtSize(written)} · 已用 ${elapsed}`;
    } else {
      detail = `正在下载分片 · 已用 ${elapsed}`;
    }

    if (pct != null) {
      setDlBusy(btn, "合并中", pct);
      showDlProgress(btn, pct / 100, detail, false);
      btn.title = `正在合并 HLS 分片 · ${pct}% · ${detail}`;
    } else {
      // 没有可靠分母：进度条走不确定态，明确表示"进度未知"而不是"卡住了"
      setDlBusy(btn, "合并中");
      showDlProgress(btn, 0, detail, true);
      btn.title = st.step || detail;
    }
  }

  function fail(err) {
    state.searching = false;
    clearTimeout(state.pollTimer);
    setBusy(false);
    show("error");
    renderError(err || { code: "unknown", message: "未知错误" });
  }

  /** 错误码 → 针对性补救建议（这是"体验"的关键一环：报错也要给出下一步）。 */
  function renderError(err) {
    el.errCode.textContent = err.code || "error";
    el.errMsg.textContent = err.message || "解析失败";

    const TIPS = {
      bad_input: ["粘贴完整链接，不要带中文分享文案", "支持 http/https 开头的地址"],
      unsupported: ["该站点没有公开的解析通道，可用 yt-dlp 支持列表核对", "也可手动复制网页里的 mp4/m3u8 地址，本页暂不做通用嗅探"],
      private: ["该视频需要登录或仅自己可见", "配置后端 VX_COOKIES_FILE 后重试"],
      login_required: ["用浏览器登录该站点 → 扩展导出 cookies.txt（Netscape 格式）", "启动前设置 VX_COOKIES_FILE=C:\\path\\cookies.txt"],
      gone: ["换一个仍在有效期内的链接", "部分站点删除/下架后不会保留可播流"],
      age_gate: ["在浏览器登录该站点并导出 cookies.txt", "启动前设置 VX_COOKIES_FILE 指向该文件"],
      blocked: ["站点风控：稍后重试，或开启后端 VX_IMPERSONATE=chrome", "部分站点需要登录 Cookie", "确认本机网络能直接打开该视频页面"],
      no_source: ["视频被加密或仅音轨，无法直接提取可播源", "付费内容需要登录态（VX_COOKIES_FILE）"],
      timeout: ["稍后重试", "网络不通或被墙：请在能访问该站点的环境运行本服务"],
      dns: ["检查链接域名是否拼写正确", "确认本机 DNS / 代理设置正常"],
      tls: ["校准系统时间", "若走了公司代理/防火墙，需信任其根证书"],
      http_429: ["提取次数过于频繁", "稍等一分钟再试"],
      extract_failed: ["站点可能改版：升级后端 yt-dlp（python -m pip install -U yt-dlp）", "确认视频链接仍然有效"],
    };
    const list = TIPS[err.code] || TIPS.extract_failed;
    const ul = document.createElement("ul");
    list.forEach((t) => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
    el.errTips.replaceChildren(ul);
  }

  /* ----------------------------------------------------------------------
     4. 结果渲染（只渲染白名单字段）
     ---------------------------------------------------------------------- */

  function render(data) {
    state.data = data;
    show("result");

    // ① 标题
    el.title.textContent = data.title || "未命名视频";
    el.title.title = "点击复制标题";

    // ② 元信息行
    el.fromName.textContent = data.provider || "通用";
    el.fromDot.style.background = data.provider_color || "var(--c-text-3)";
    el.duration.textContent = data.is_live ? "直播中" : fmtDuration(data.duration);
    el.adBadge.hidden = !data.filtered || !data.filtered.removed_ads;

    // ③ 封面 + 播放器
    if (data.thumbnail) {
      el.posterImg.src = data.thumbnail;
      el.poster.hidden = false;
      el.poster.classList.remove("is-hidden");
    } else {
      el.poster.hidden = true;
    }
    el.video.removeAttribute("src");
    el.video.load();
    el.note.hidden = true;
    el.player.classList.remove("is-portrait");

    // ④ 直链列表 + 清晰度菜单
    renderSources(data.sources || []);
    state.current = -1;
    const idx = pickDefault(data);
    if (idx >= 0) setSource(idx, { autoplay: false });
    scrollToResult();
  }

  function pickDefault(data) {
    const list = data.sources || [];
    if (!list.length) return -1;
    const i = data.default_index;
    return (typeof i === "number" && list[i]) ? i : 0;
  }

  /** 渲染直链区与清晰度浮层。 */
  function renderSources(sources) {
    state.remuxGen += 1;          // 作废所有进行中的合并轮询（按钮随之消失）
    el.linksList.replaceChildren();
    el.qualityList.replaceChildren();
    el.quality.hidden = sources.length <= 1;

    sources.forEach((s, i) => {
      // ---- 直链卡片 ----
      const node = el.tplLink.content.cloneNode(true);
      const root = node.querySelector(".vl-link");
      root.dataset.idx = String(i);

      root.querySelector(".vl-link__label").textContent =
        s.label || (s.height ? s.height + "P" : "来源 " + (i + 1));

      const tags = root.querySelector(".vl-link__tags");
      const tag = (text, cls) => {
        const span = document.createElement("span");
        span.className = "vl-mini-tag" + (cls ? " " + cls : "");
        span.textContent = text;
        tags.appendChild(span);
      };
      tag(s.is_hls ? "HLS" : (s.ext || "mp4").toUpperCase(),
          s.is_hls ? "vl-mini-tag--hls" : "vl-mini-tag--mp4");
      if (s.fps && s.fps > 30) tag(Math.round(s.fps) + "fps");
      if (s.bitrate_kbps) tag(Math.round(s.bitrate_kbps) + " kbps");
      // 体积：filesize 是站点给的确定值；size_estimate_bytes 是我们按码率×时长算的
      // 预估值。两者用不同后缀区分（"约 xx"），不把估算冒充成精确值。
      if (s.size_bytes) {
        tag(fmtSize(s.size_bytes));
      } else if (s.size_estimate_bytes) {
        tag("约 " + fmtSize(s.size_estimate_bytes), "vl-mini-tag--est");
      }
      if (s.requires_headers) tag("带防盗链");

      const urlBox = root.querySelector(".vl-link__url");
      urlBox.textContent = s.direct_url;
      urlBox.title = s.direct_url + "\n\n点击复制";
      urlBox.addEventListener("click", async () => {
        const ok = await copyText(s.direct_url);
        urlBox.classList.toggle("is-copied", ok);
        toast(ok ? "已复制该视频直链" : "复制失败，请手动选中复制", ok ? "ok" : "err");
        setTimeout(() => urlBox.classList.remove("is-copied"), 1600);
      });

      root.querySelector(".vl-btn--play").addEventListener("click", () => setSource(i, { autoplay: true }));
      root.querySelector(".vl-btn--copy").addEventListener("click", async () => {
        const ok = await copyText(s.direct_url);
        toast(ok ? "已复制该视频直链" : "复制失败", ok ? "ok" : "err");
      });
      const openBtn = root.querySelector(".vl-btn--open");
      if (s.is_hls && state.ffmpeg) {
        // m3u8 是「分片清单」，直接存盘只是一份打不开的文本 —— 先让后端 ffmpeg 合并成 MP4。
        // 交互只改这一个按钮的文案 + 一条轻提示，界面上不新增任何面板。
        setDlIdle(openBtn, s, i);
        openBtn.addEventListener("click", (ev) => {
          // 只有 idle 态才是「发起合并」，此时必须拦住默认跳转；
          // ready 态要让 <a href download> 的原生下载生效 —— 无条件 preventDefault
          // 会把「保存 MP4」点成没反应（这正是之前的 bug）。
          if (openBtn.dataset.mode !== "idle") return;
          ev.preventDefault();
          startRemux(s, i, openBtn);
        });
      } else {
        // 非 HLS（或本机无 ffmpeg 的降级）：走本机代理，同源 download 属性才生效，
        // 且代理会自动补上防盗链头
        openBtn.href = s.play_url + "&dl=1";
        openBtn.setAttribute("download", filenameFor(i, s));
        openBtn.textContent = "下载";
        if (s.is_hls) {
          openBtn.title = "本机未安装 ffmpeg：这里下载的是 m3u8 清单，分片仍需联网拉取，"
                        + "请把这条直链交给 VLC / mpv 打开。安装 ffmpeg 并重启服务后即可在此直接合并成 MP4";
        }
      }

      el.linksList.appendChild(node);

      // ---- 清晰度菜单项 ----
      const q = el.tplQuality.content.cloneNode(true);
      const item = q.querySelector(".vl-quality-item");
      item.dataset.idx = String(i);
      item.addEventListener("click", () => {
        setSource(i, { autoplay: true });
        closeQualityMenu();
      });
      item.querySelector(".vl-quality-item__label").textContent = s.label || "来源 " + (i + 1);
      item.querySelector(".vl-quality-item__sub").textContent =
        (s.is_hls ? "HLS" : (s.ext || "").toUpperCase());
      el.qualityList.appendChild(q);
    });
  }

  /** 高亮当前源。 */
  function markActive(i) {
    document.querySelectorAll(".vl-link.is-active, .vl-quality-item.is-active")
      .forEach((n) => n.classList.remove("is-active"));
    const card = el.linksList.querySelector(`.vl-link[data-idx="${i}"]`);
    const item = el.qualityList.querySelector(`.vl-quality-item[data-idx="${i}"]`);
    if (card) card.classList.add("is-active");
    if (item) item.classList.add("is-active");
    const s = (state.data && state.data.sources) ? state.data.sources[i] : null;
    el.qualityCur.textContent = s ? (s.label || "已选") : "自动";
  }

  /* ----------------------------------------------------------------------
     5. 播放：原生 mp4 / hls.js 分流
     ---------------------------------------------------------------------- */

  function stopHls() {
    if (state.hls) {
      try { state.hls.destroy(); } catch (_) { /* 忽略销毁异常 */ }
      state.hls = null;
    }
    el.video.pause();
    el.video.removeAttribute("src");
    el.video.load();
  }

  /**
   * 切换播放源。
   * 关键决策：
   *   - m3u8 → 用 play_url（后端已改写清单里的分片地址）
   *   - Safari 原生支持 HLS → 直接 src，不加载 hls.js
   *   - mp4/webm → 直接 src
   */
  function setSource(index, opts) {
    const data = state.data;
    if (!data || !data.sources || !data.sources[index]) return;
    const src = data.sources[index];
    const autoplay = opts && opts.autoplay;

    stopHls();
    state.current = index;
    markActive(index);
    el.note.hidden = true;

    const abs = (u) => new URL(u, location.href).href;   // 代理地址是相对路径，转成绝对值

    if (src.is_hls) {
      const nativeHls = el.video.canPlayType("application/vnd.apple.mpegurl") !== "";
      if (nativeHls) {
        el.video.src = abs(src.play_url);
      } else if (window.Hls && window.Hls.isSupported()) {
        const hls = new window.Hls({
          // 清单已被本服务改写为同源代理，无需再配 xhr 头
          maxBufferLength: 30,
          lowLatencyMode: false,
          enableWorker: true,
        });
        hls.loadSource(abs(src.play_url));
        hls.attachMedia(el.video);
        hls.on(window.Hls.Events.ERROR, (_e, info) => {
          if (!info.fatal) return;
          if (info.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
            el.note.hidden = false;
            el.note.textContent = "HLS 分片读取中断：直链可能已过期，请重新提取";
          } else if (info.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();          // 解码类错误先自愈一次
          } else {
            el.note.hidden = false;
            el.note.textContent = "该 HLS 源无法在当前浏览器播放，可切换到其它清晰度或复制直链";
          }
        });
        state.hls = hls;
      } else {
        el.note.hidden = false;
        el.note.textContent = "当前浏览器不支持 HLS 播放，请复制直链到外部播放器";
      }
    } else {
      el.video.src = abs(src.play_url);
    }

    // 失败兜底：明确告诉用户下一步做什么，而不是留一个转圈的播放器
    el.video.onerror = () => {
      el.note.hidden = false;
      el.note.textContent = "播放失败：代理直链已失效，请点击「重新提取」获取新直链";
    };

    if (autoplay) {
      revealPoster();
      const p = el.video.play();
      if (p && p.catch) p.catch(() => { /* 自动播放被拦截：用户手动点一次即可 */ });
    }
  }

  function revealPoster() {
    el.poster.classList.add("is-hidden");
    setTimeout(() => { el.poster.hidden = true; }, 240);
  }

  /* ----------------------------------------------------------------------
     6. 浮层菜单
     ---------------------------------------------------------------------- */
  function closeQualityMenu() {
    el.quality.classList.remove("is-open");
    el.qualityBtn.setAttribute("aria-expanded", "false");
  }
  function toggleQualityMenu() {
    const open = !el.quality.classList.contains("is-open");
    el.quality.classList.toggle("is-open", open);
    el.qualityBtn.setAttribute("aria-expanded", String(open));
  }

  /* ----------------------------------------------------------------------
     7. 事件绑定
     ---------------------------------------------------------------------- */

  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    startExtract(el.url.value);
  });

  el.paste.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      const match = (text || "").match(/https?:\/\/[^\s"'<>"，。；]+/i);
      if (match) {
        el.url.value = match[0];
        startExtract(match[0]);
      } else {
        toast("剪贴板里没找到链接", "warn");
      }
    } catch (_) {
      toast("浏览器未授权读取剪贴板，请手动粘贴（Ctrl+V）", "warn");
      el.url.focus();
    }
  });

  el.cancel.addEventListener("click", () => {
    state.searching = false;
    clearTimeout(state.pollTimer);
    setBusy(false);
    show(state.data ? "result" : "empty");
  });

  el.retry.addEventListener("click", () => startExtract(state.lastUrl || el.url.value));
  el.reset.addEventListener("click", () => show(state.data ? "result" : "empty"));

  el.title.addEventListener("dblclick", async () => {
    const ok = await copyText(el.title.textContent);
    toast(ok ? "已复制标题" : "复制失败", ok ? "ok" : "err");
  });

  el.bigPlay.addEventListener("click", () => {
    revealPoster();
    if (state.current < 0) {
      const idx = pickDefault(state.data);
      if (idx >= 0) { setSource(idx, { autoplay: true }); return; }
    }
    el.video.play().catch(() => toast("浏览器阻止了自动播放，请再点一次", "warn"));
  });

  el.video.addEventListener("play", revealPoster);
  el.video.addEventListener("loadedmetadata", () => {
    // 竖屏内容（抖音/小红书）：收窄画面区域而不是拉黑边
    const portrait = el.video.videoHeight > el.video.videoWidth * 1.1;
    el.player.classList.toggle("is-portrait", portrait);
  });
  el.video.addEventListener("waiting", () => { el.note.hidden = false; el.note.textContent = "缓冲中…"; });
  el.video.addEventListener("playing", () => { el.note.hidden = true; });

  el.qualityBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleQualityMenu(); });
  document.addEventListener("click", (e) => {
    if (!el.quality.contains(e.target)) closeQualityMenu();
  });

  el.copyAll.addEventListener("click", async () => {
    const list = (state.data && state.data.sources) || [];
    if (!list.length) return;
    const text = list.map((s, i) => `${i + 1}. ${s.label || "来源"}\n   ${s.direct_url}`).join("\n");
    const ok = await copyText(text);
    toast(ok ? `已复制 ${list.length} 条直链` : "复制失败", ok ? "ok" : "err");
  });

  el.exportJson.addEventListener("click", () => {
    if (!state.data) return;
    const dump = {
      url: state.lastUrl,
      exported_at: new Date().toISOString(),
      title: state.data.title,
      provider: state.data.provider,
      duration: state.data.duration,
      sources: state.data.sources.map((s) => ({
        label: s.label, ext: s.ext, protocol: s.protocol,
        height: s.height, bitrate_kbps: s.bitrate_kbps, url: s.direct_url,
      })),
    };
    const blob = new Blob([JSON.stringify(dump, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (state.data.title || "video").slice(0, 40).replace(/[\\/:*?"<>|]/g, "_") + ".json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });

  // 全局快捷键
  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable;

    if (e.key === "Escape") { closeQualityMenu(); return; }

    // Ctrl/Cmd+V 在任意位置：识别到链接就直接开始提取
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v" && !typing && !state.searching) {
      navigator.clipboard && navigator.clipboard.readText().then((text) => {
        const m = (text || "").match(/https?:\/\/[^\s"'<>"，。；]+/i);
        if (m) { el.url.value = m[0]; startExtract(m[0]); }
      }).catch(() => { /* 无剪贴板权限：静默，用户仍可手动粘贴 */ });
      return;
    }

    // 非输入态下 Space 控制播放
    if (e.code === "Space" && !typing && !el.result.hidden) {
      e.preventDefault();
      if (el.video.paused) { revealPoster(); el.video.play().catch(() => {}); }
      else el.video.pause();
    }
  });

  function scrollToResult() {
    // 结果出现后把播放器带到视野内（移动端键盘收起时尤为重要）
    setTimeout(() => {
      if (window.innerWidth < 880) el.player.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 60);
  }

  /* ----------------------------------------------------------------------
     8. 初始化
     ---------------------------------------------------------------------- */
  function init() {
    // 站点标签
    SITES.forEach((name) => {
      const li = document.createElement("li");
      li.className = "vl-tag";
      li.textContent = name;
      el.siteTags.appendChild(li);
    });

    // URL 里带 ?u= / # 直接开提：便于做浏览器「搜索引擎」或分享转发
    const q = new URLSearchParams(location.search).get("u");
    const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
    const prefill = q || (/^https?:\/\//i.test(hash) ? hash : "");
    if (prefill) {
      el.url.value = prefill;
      startExtract(prefill);
    } else {
      el.url.focus();
      show("empty");
    }

    // 健康检查：后端没起就直接告知，而不是让用户点了「提取」才发现
    api("/api/health").then((h) => {
      el.health.hidden = false;
      el.healthLed.dataset.ok = "1";
      el.healthText.textContent = "服务正常 · v" + (h.version || "");
      el.footVer.textContent = "v" + (h.version || "");
      // 能力位决定 m3u8 的「下载」是合并成 MP4 还是原样存清单；
      // 结果若已先一步渲染（?u= 直达），按真实能力重画直链区
      state.ffmpeg = !!h.ffmpeg_available;
      if (state.data && state.data.sources) {
        renderSources(state.data.sources);
        if (state.current >= 0) markActive(state.current);
      }
    }).catch(() => {
      el.health.hidden = false;
      el.healthLed.dataset.ok = "0";
      el.healthText.textContent = "后端未连接";
      toast("后端服务未启动：请执行 python backend/run.py", "err");
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
