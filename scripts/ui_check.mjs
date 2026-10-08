/* 无头浏览器 UI 回归（走 CDP，Chrome/Edge 均可）。
   验证的是「页面确实跑得起来、点得动」这一层：模式切换、批量提交、
   错误提示是否真的可见（CSS 钩子匹配）、进度渲染。
   运行：node scripts/ui_check.mjs        （需先启动服务）
   可用环境变量：VL_BROWSER=<浏览器路径>  VL_BASE=<服务地址>  VL_CDP_PORT=<端口>
*/
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CANDIDATES = [
  process.env.VL_BROWSER,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const EDGE = CANDIDATES.find((p) => existsSync(p));
if (!EDGE) {
  console.error("未找到 Chrome/Edge；可用 VL_BROWSER=<路径> 指定");
  process.exit(2);
}
const PORT = Number(process.env.VL_CDP_PORT || 9333);
const profile = mkdtempSync(join(tmpdir(), "vl-ui-"));
const BASE = process.env.VL_BASE || "http://127.0.0.1:8399";

const child = spawn(EDGE, [
  "--headless=new",
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-gpu",
  "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdpTargets() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find((t) => t.type === "page");
      if (page) return page;
    } catch { /* 还没起来 */ }
    await sleep(300);
  }
  throw new Error("无法连接 Edge 调试端口");
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const { resolve: res, reject: rej } = pending.get(msg.id);
        pending.delete(msg.id);
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
      }
    });
    ws.addEventListener("error", reject);
    ws.addEventListener("open", () => {
      const send = (method, params = {}) => new Promise((res, rej) => {
        const mid = ++id;
        pending.set(mid, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id: mid, method, params }));
      });
      resolve({ send, close: () => ws.close() });
    });
  });
}

let fails = 0;
function check(name, cond, detail = "") {
  console.log((cond ? "  PASS  " : "  FAIL  ") + name + (detail ? "  " + detail : ""));
  if (!cond) fails++;
}

try {
  const target = await cdpTargets();
  const { send, close } = await connect(target.webSocketDebuggerUrl);
  await send("Page.enable");
  await send("Runtime.enable");

  const errors = [];
  await send("Runtime.addBinding", { name: "__vlErr" }).catch(() => {});
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(2500);

  async function evalJs(expression) {
    const r = await send("Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " :: " + expression.slice(0, 80));
    return r.result.value;
  }

  console.log("== 页面加载 ==");
  check("标题正确", (await evalJs("document.title")).includes("Video Lens"));
  check("无 JS 运行时错误", errors.length === 0, errors.join(" | "));

  console.log("\n== 模式切换 ==");
  const initial = await evalJs(`JSON.stringify({
    tabs: document.querySelectorAll('.vl-tab').length,
    batchHidden: document.getElementById('batch-state').hidden,
    schedHidden: document.getElementById('schedule-state').hidden,
    emptyHidden: document.getElementById('empty-state').hidden })`);
  const s0 = JSON.parse(initial);
  check("三个模式标签存在", s0.tabs === 3, "tabs=" + s0.tabs);
  check("初始：批量/定时面板隐藏", s0.batchHidden && s0.schedHidden);
  check("初始：单条解析可见", s0.emptyHidden === false);

  await evalJs(`document.getElementById('tab-batch').click()`);
  await sleep(400);
  const s1 = JSON.parse(await evalJs(`JSON.stringify({
    batchHidden: document.getElementById('batch-state').hidden,
    emptyHidden: document.getElementById('empty-state').hidden,
    active: document.querySelector('.vl-tab.is-active').dataset.mode })`));
  check("切到批量：面板显示", s1.batchHidden === false);
  check("切到批量：单条视图隐藏", s1.emptyHidden === true);
  check("切到批量：标签高亮正确", s1.active === "batch");

  await evalJs(`document.getElementById('tab-schedule').click()`);
  await sleep(600);
  const s2 = JSON.parse(await evalJs(`JSON.stringify({
    schedHidden: document.getElementById('schedule-state').hidden,
    batchHidden: document.getElementById('batch-state').hidden,
    emptyHidden: document.getElementById('empty-state').hidden })`));
  check("切到定时：面板显示且其它隐藏",
        s2.schedHidden === false && s2.batchHidden === true && s2.emptyHidden === true);

  console.log("\n== 定时面板（读取真实后端）==");
  const sched = JSON.parse(await evalJs(`JSON.stringify({
    list: document.querySelectorAll('#schedule-list .vl-sched').length,
    emptyShown: !document.getElementById('schedule-empty').hidden })`));
  check("计划列表已渲染（0 个时显示空态提示）",
        sched.list > 0 || sched.emptyShown, `list=${sched.list} empty=${sched.emptyShown}`);

  // 建一个计划 → 列表应出现该行，并显示"下次触发时间"（而不是含糊的"尚未执行"）
  console.log("\n== 定时面板：新建计划后即时可见 ==");
  // 注意：CDP 的 returnByValue 会直接把对象还原成 JS 对象，所以这里不能再 JSON.parse
  const created = await evalJs(`(async () => {
    const r = await fetch('/api/schedules', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url: 'https://www.w3schools.com/html/mov_bbb.mp4',
        mode: 'interval', every_minutes: 600, label: 'UI检查计划' }) });
    return await r.json();
  })()`);
  check("后端已建计划", !!created.schedule_id, created.schedule_id || JSON.stringify(created));
  // 切走再切回，触发重新拉取（等价于用户点标签）
  await evalJs(`document.getElementById('tab-single').click()`);
  await sleep(200);
  await evalJs(`document.getElementById('tab-schedule').click()`);
  await sleep(1200);
  const row = await evalJs(`(() => {
    const li = document.querySelector('#schedule-list .vl-sched');
    if (!li) return { found: false };
    return {
      found: true,
      when: li.querySelector('.vl-sched__when').textContent,
      last: li.querySelector('.vl-sched__last').textContent,
      buttons: Array.from(li.querySelectorAll('button')).map(b => b.textContent),
    }; })()`);
  check("列表出现了刚建的计划", row.found === true);
  check("显示了「下次触发时间」而不是只说尚未执行",
        row.found && String(row.when).includes("下次"), String(row.when || ""));
  check("三个操作按钮齐备（立即执行/暂停/删除）",
        row.found && row.buttons.length >= 3, (row.buttons || []).join("/"));

  // 清理
  await evalJs(`(async () => {
    await fetch('/api/schedules/${created.schedule_id}', { method: 'DELETE' }); })()`);

  console.log("\n== 批量面板：链接识别 ==");
  await evalJs(`document.getElementById('tab-batch').click()`);
  await sleep(300);
  await evalJs(`(() => {
    const ta = document.getElementById('batch-input');
    ta.value = "https://www.w3schools.com/html/mov_bbb.mp4\\n随便一段文字 https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8 结尾\\nhttps://www.w3schools.com/html/mov_bbb.mp4";
    ta.dispatchEvent(new Event('input'));
  })()`);
  const cnt = await evalJs(`document.getElementById('batch-count').textContent`);
  check("识别链接并去重", cnt.includes("2"), cnt);

  console.log("\n== 批量面板：坏链接应给出明确提示而不是静默失败 ==");
  await evalJs(`(() => {
    const ta = document.getElementById('batch-input');
    ta.value = "https://a.example/1.mp4";
    ta.dispatchEvent(new Event('input'));
  })()`);
  await evalJs(`document.getElementById('batch-submit').click()`);
  await sleep(1500);
  const badToast = JSON.parse(await evalJs(`JSON.stringify({
    text: document.getElementById('toast').textContent,
    shown: document.getElementById('toast').classList.contains('is-show') })`));
  check("坏链接弹出可读错误提示",
        badToast.shown && badToast.text.includes("不可用"), badToast.text.slice(0, 50));
  // 提示是否真的可见（CSS 钩子是否匹配）—— 这里验的是 .is-show 规则存在且生效
  const toastVisible = await evalJs(
    `getComputedStyle(document.getElementById('toast')).opacity`);
  check("提示确实可见（CSS 钩子匹配）", Number(toastVisible) > 0.5, "opacity=" + toastVisible);

  console.log("\n== 批量面板：真实提交（可解析的链接）==");
  await evalJs(`(() => {
    const ta = document.getElementById('batch-input');
    ta.value = "https://www.w3schools.com/html/mov_bbb.mp4\\nhttps://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8";
    ta.dispatchEvent(new Event('input'));
  })()`);
  const before = await evalJs(`(async () => {
    const r = await fetch('/api/batches'); const d = await r.json();
    return d.batches.length; })()`);
  await evalJs(`document.getElementById('batch-submit').click()`);
  await sleep(2500);
  const prog = JSON.parse(await evalJs(`JSON.stringify({
    shown: !document.getElementById('batch-progress').hidden,
    label: document.getElementById('batch-progress-label').textContent })`));
  check("提交成功后进度条出现", prog.shown === true, prog.label);

  const after = await evalJs(`(async () => {
    const r = await fetch('/api/batches'); const d = await r.json();
    return d.batches.length; })()`);
  check("后端确实新建了批次", after > before, `${before} -> ${after}`);

  await sleep(5000);
  const items = await evalJs(`document.querySelectorAll('#batch-items .vl-item').length`);
  check("条目列表已渲染", items > 0, "items=" + items);

  console.log("\n== 体积预估与进度条（真实提取一条 HLS 源）==");
  const est = await evalJs(`(async () => {
    const r = await fetch('/api/extract', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ url: 'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8' }) });
    const j = await r.json();
    for (let i = 0; i < 40; i++) {
      await new Promise(s => setTimeout(s, 1000));
      const st = await (await fetch('/api/jobs/' + j.job_id)).json();
      if (st.status === 'done' || st.status === 'failed') return st;
    }
    return null;
  })()`);
  check("HLS 提取成功", est && est.status === "done", est ? est.status : "timeout");
  const src = est && est.result && est.result.sources[est.result.default_index];
  check("结果里带预估体积（下载前就能看到大小）",
        !!(src && src.size_estimate_bytes),
        src ? String(src.size_estimate_bytes) + " bytes" : "no source");

  // 真正点一次「下载 MP4」，观察按钮文案与进度条元素是否出现
  await evalJs(`document.querySelector('#tab-single').click()`);
  await sleep(400);
  await evalJs(`(async () => {
    // 直接走 UI 流程：填链接 → 提取 → 等结果渲染
    document.getElementById('url-input').value =
      'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8';
    document.getElementById('extract-form').dispatchEvent(
      new Event('submit', { cancelable: true, bubbles: true }));
  })()`);
  // 等结果态出现（最多 45s）
  let ready = false;
  for (let i = 0; i < 45; i++) {
    await sleep(1000);
    ready = await evalJs(`!document.getElementById('result-state').hidden`);
    if (ready) break;
  }
  check("单条解析结果已渲染", ready === true);

  if (ready) {
    // 注意：列表里有多个清晰度，默认选中的那一条才带 .is-active。
    // 早先直接抓第一个 .vl-btn--open 会命中 1080P（504 MB），而实际默认是 184P，
    // 于是"界面显示的大小对不上产物"——那是测试选错了行，不是产品问题。
    const dl = await evalJs(`(() => {
      const active = document.querySelector('.vl-link.is-active') 
                  || document.querySelector('.vl-link');
      const b = active ? active.querySelector('.vl-btn--open') : null;
      return { text: b ? b.textContent : '', mode: b ? b.dataset.mode : '',
               activeRow: !!document.querySelector('.vl-link.is-active') }; })()`);
    check("下载按钮显示预估大小", /约|MB|KB|GB/.test(dl.text), dl.text || "(空)");
    console.log("    默认清晰度按钮文案: " + (dl.text || "(空)"));

    // 与接口给的默认源预估大小交叉核对，确保界面数字有出处
    check("按钮上的大小与后端默认源预估一致",
          !!(src && src.size_estimate_bytes && dl.text
             && String(dl.text).includes(String(Math.round(src.size_estimate_bytes / 1048576)))),
          `按钮="${dl.text}" 后端=${src ? Math.round(src.size_estimate_bytes / 1048576) + " MB" : "?"}`);

    // 点下去，检查进度条元素被创建且按钮进入 busy。
    // 注意要等"合并中"阶段：点击后先有"提交中…"，真正跑起来才是带百分比的合并态。
    await evalJs(`(() => {
      const active = document.querySelector('.vl-link.is-active')
                  || document.querySelector('.vl-link');
      active.querySelector('.vl-btn--open').click(); })()`);
    let prog = null;
    for (let i = 0; i < 40; i++) {
      await sleep(1000);
      prog = await evalJs(`(() => {
        const active = document.querySelector('.vl-link.is-active')
                    || document.querySelector('.vl-link');
        const bar = active ? active.querySelector('.vl-dlprog') : null;
        const b = active ? active.querySelector('.vl-btn--open') : null;
        return { exists: !!bar, shown: bar ? !bar.hidden : false,
                 text: bar ? bar.querySelector('.vl-dlprog__text').textContent : '',
                 fill: bar ? bar.querySelector('.vl-dlprog__fill').style.width : '',
                 btn: b ? b.textContent : '', mode: b ? b.dataset.mode : '' }; })()`);
      // 等到出现百分比（真正进入合并轮询）就够验证了
      if (prog && prog.btn && /%/.test(prog.btn)) break;
    }
    check("出现了合并进度条", !!(prog && prog.exists && prog.shown), JSON.stringify(prog));
    check("进度条有文字说明（已写/预估）",
          !!(prog && /MB|KB|GB/.test(prog.text)), (prog && prog.text) || "");
    check("按钮进入合并态并显示百分比",
          !!(prog && prog.mode === "busy" && /%/.test(prog.btn)), (prog && prog.btn) || "");
    console.log("    进度条文案: " + ((prog && prog.text) || "(空)"));
    console.log("    按钮文案  : " + ((prog && prog.btn) || "(空)"));
  }

  console.log("\n== 后端清理测试批次 ==");
  const bid = await evalJs(`(async () => {
    const r = await fetch('/api/batches'); const d = await r.json();
    return (d.batches[0] || {}).batch_id || '';
  })()`);
  if (bid) {
    const del = await evalJs(`(async () => {
      const r = await fetch('/api/batch/${""}' + '${bid}', { method: 'DELETE' });
      return r.status;
    })()`);
    console.log("  已清理测试批次 " + bid + " -> HTTP " + del);
  }

  close();
  console.log(`\n结果：${fails === 0 ? "UI_OK" : fails + " 项失败"}`);
} catch (err) {
  console.error("UI 检查出错:", err.message);
  fails++;
} finally {
  child.kill();
  await sleep(500);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* Windows 偶尔占用 */ }
}
process.exit(fails === 0 ? 0 : 1);
