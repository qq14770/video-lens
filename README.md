# Video Lens —— 视频内容提取 Web UI

聚焦视频本身：粘贴主流视频站分享链接，界面**只呈现三样东西 —— 视频标题、播放器、提取到的视频直链**。
广告、贴片流、DRM 流、UP 主信息、互动数据、推荐列表等一切冗余元素，在**后端白名单投影层**即被丢弃，
前端结构上不可能渲染出它们。

```
video-extract/
├── backend/                 # Python · FastAPI · yt-dlp
│   ├── run.py               # 一键启动入口
│   ├── requirements.txt     # 依赖清单
│   └── app/
│       ├── main.py          # 路由装配（REST API + 静态托管）
│       ├── config.py        # 环境变量配置（VX_* 前缀）
│       ├── extractor.py     # 核心解析 + 广告/冗余过滤（白名单投影）
│       ├── jobs.py          # 异步任务表（LRU + TTL）
│       ├── security.py      # SSRF 防护 + HMAC 代理令牌
│       ├── stream_proxy.py  # 直链反向代理（Range 透传 / m3u8 改写）
│       ├── scraper.py       # 页面兜底解析（无提取器站点：读 og:title + 扫播放直链）
│       ├── remux.py         # HLS→MP4 服务端合并（ffmpeg -c copy，解决「m3u8 存盘是废文件」）
│       ├── playlist.py      # 清单分析：合并前预估体积 + 合并中百分比进度的分母
│       ├── batch.py         # 批量下载（多链接逐条处理 → 打包一个 zip）
│       ├── scheduler.py     # 定时任务调度（daily / interval，到点自动跑）
│       ├── store.py         # 计划与批次的持久化（JSON + 原子写入，重启不丢）
│       └── providers.py     # 站点识别（名称 + 品牌色）
├── frontend/                # 原生 HTML/CSS/JS，零框架零构建
│   ├── index.html           # 单页骨架（单条解析四态 + 批量面板 + 定时面板）
│   ├── css/style.css        # 暗色主题设计令牌 + 布局
│   ├── js/app.js            # 单条解析状态机 + API 对接 + 播放器控制
│   ├── js/panels.js         # 批量下载与定时任务（独立模块，不碰单条解析状态机）
│   └── vendor/hls.min.js    # HLS 播放支持（本地 vendor，无 CDN 依赖）
├── docker/                  # 容器辅助脚本
│   ├── preflight.py         # 启动自检（依赖/ffmpeg/静态资源/目录可写）
│   ├── healthcheck.py       # 健康检查（打 /api/health，含 ffmpeg 能力位）
│   └── link_install.py      # 把 --prefix 安装的依赖接进 sys.path（构建期用）
├── scripts/
│   ├── smoke.py             # 端到端冒烟测试（85 项断言）
│   ├── ui_check.mjs         # 无头浏览器 UI 回归（模式切换/批量提交/提示可见性）
│   ├── sim_image_layout.py  # 按镜像目录结构起服务，验证静态资源与数据卷路径
│   ├── fix-perms.sh         # 修 Windows 拷过来的空权限位（NAS 部署必看）
│   ├── check-deploy.sh      # 构建前部署自检（飞牛上跑：查缺文件/旧镜像/数据目录）
│   ├── check_shell.py       # 审计递归权限脚本的安全性（bash -n + 危险路径黑名单）
│   ├── diag_container.py    # 容器启动失败排查助手
│   └── check_frontend.py    # 前端静态一致性校验（DOM id/class/模板对齐）
├── Dockerfile               # 容器镜像（内置 ffmpeg，非 root 运行）
├── docker-compose.yml       # 一键部署（含飞牛 OS 的路径与端口说明）
├── DEPLOY-fnOS.md           # 飞牛 OS 部署指南（含常见问题排查）
└── README.md                # 本文档
```

---

## 一、功能需求梳理

### 1.1 核心需求

| # | 需求 | 落地方式 |
|---|------|---------|
| R1 | 输入主流视频站分享链接，提取视频直链 | `POST /api/extract` → yt-dlp 解析（YouTube / B站 / 抖音 / X / Vimeo 等上千站点） |
| R2 | **过滤所有广告与冗余元素** | 后端白名单投影（见 1.2），广告流与多余字段不出后端 |
| R3 | 界面仅展示：标题 + 播放器 + 直链 | 单页四态：空态 → 加载 → 结果 → 错误，结果区只有这三块 |
| R4 | 提取到的直链要能**直接用** | 每条源三个动作：「播放」（本页切源）/「复制链接」（原始直链）/「下载」。**非 HLS**：走代理 `dl=1` 附件，自动带防盗链头。**HLS/m3u8**：存清单本身是打不开的废文件，故本机有 ffmpeg 时「下载」改为服务端合并（`POST /api/remux` → `ffmpeg -c copy` → 存成 MP4，见 4.2b）；没有 ffmpeg 才降级为存原始清单（分片仍需联网拉取，按钮 title 里提示可把直链交给 VLC/mpv）。直链文本本身单击亦可复制 |
| R5 | 网页里点了就能播（不跳源站） | 反向代理 `/api/stream` 解决 CORS、Referer 防盗链、混合内容三大障碍 |
| R6 | 进度可见、失败可读 | 任务化异步 + 轮询进度文案；错误翻译成「下一步建议」而非堆栈 |
| R7 | **批量下载**：一次多条链接，一个 zip 取走 | `POST /api/batch` 逐条提取（HLS 自动合并）→ 打包 zip；见 4.2c |
| R8 | **定时任务**：到点自动跑，重启不丢 | `scheduler.py` + `store.py`（JSON 原子落盘）；支持「每天某时刻」「每隔 N 分钟」；见 4.2d |
| R9 | **下载前知道多大 + 下载中看到进度** | `playlist.py` 分析清单得「码率 × 时长」预估体积（实测误差 ~3%）；合并过程按 ffmpeg 已写字节上报百分比；见 4.2e |

### 1.2 「过滤广告与冗余」的两条落点（设计关键）

**落点一：数据层白名单投影（`extractor.py`）**
yt-dlp 原始 info 有 100+ 字段（描述、标签、UP 主、播放量、点赞、推荐列表、弹幕接口、贴片广告清单……）。
`extract()` 只投影 UI 需要的字段，其余一律不出后端：

```jsonc
// 前端拿到的一切（字段级白名单）
{
  "title": "...", "provider": "哔哩哔哩", "provider_color": "#fb7299",
  "duration": 213, "thumbnail": "…", "is_live": false,
  "sources": [{ "label": "1080P 蓝光 · AVC1/MP4A", "height": 1080, "ext": "mp4",
                "protocol": "http", "is_hls": false, "bitrate_kbps": 1800,
                "size_bytes": 50331648, "direct_url": "原始直链", "play_url": "/api/stream?…" }],
  "default_index": 0, "filtered": { "removed_ads": true, "max_sources": 6 }
}
```

冒烟测试对此有**结构性断言**：结果里出现 `description / uploader / view_count / tags` 等任一冗余字段即判失败。

**落点二：流层过滤（`_pick_sources`）**
对 yt-dlp 返回的每条 format 依次执行：

1. **广告流剔除** —— `format_note`/URL 命中 `ad|pre-roll|mid-roll|bumper|贴片|前插|广告` 即丢；
2. **DRM 流剔除** —— `has_drm/license_url` 浏览器播不了，展示出来只会误导；
3. **不可播流剔除** —— 单轨 DASH 分片（纯视频/纯音频）、`ism/rtmp` 等不支持协议、明确 `vcodec=none` 的纯音频；
4. **去重** —— 同一 `(高度, 视频编码, 封装)` 只留码率最高的一条；
5. **截断** —— 最多 6 条清晰度，防止列表退化成「清晰度矩阵」噪音。

### 1.3 非功能需求

- **安全**：SSRF 防护（仅 http/https、禁 userinfo、解析后 IP 必须公网）；代理链接 HMAC 签名，防本机服务被当开放转发器；每 IP 提取限流。
- **健壮**：解析看门狗（45s 超时判失败）；直链过期（站点签名时效）时给出「重新提取」引导；任务表 LRU+TTL 不泄漏内存。
- **零构建前端**：原生三件套，同源部署，打开即用；离线可用（hls.js 走本地 vendor）。

---

## 二、界面布局设计与开发指南

### 2.1 布局结构（单列聚焦式，自上而下）

```
┌────────────────────────────────────────────────┐
│ ⛶ Video Lens                  ● 服务正常        │  顶栏：品牌 + 副标语「标题·播放器·直链」
│   标题 · 播放器 · 直链          + 健康状态灯      │
├────────────────────────────────────────────────┤
│ ┌────────────────────────────────────────────┐ │
│ │  粘贴视频链接（B站/抖音/YouTube…） [粘贴][提取]│ │  输入条：唯一的用户动作入口
│ └────────────────────────────────────────────┘ │  下方提示：广告/推荐/评论/弹幕一律不加载
│                                                │
│  —— 四种互斥的界面状态，同一时刻只显示其一 ——    │
│                                                │
│  ① 空态     大播放键图标 +「粘贴链接，只留下视频」  │
│             + 支持站点标签行                      │
│  ② 加载态   步骤文案 + 时间轴进度条 + [取消]      │
│  ③ 错误态   错误码徽标 + 可读原因 + 下一步建议    │
│  ④ 结果态 ↓                                     │
│ ┌────────────────────────────────────────────┐ │
│ │  视频标题（大字号，一行，双击可复制）           │ │  ← ① 标题
│ ├────────────────────────────────────────────┤ │
│ │                                            │ │
│ │           播放器（16:9，本页直接播）          │ │  ← ② 播放器
│ │      封面遮罩+大播放键 · [清晰度 ▾] 浮于右上   │ │
│ ├────────────────────────────────────────────┤ │
│ │  ◉ Bilibili · 03:33        [已过滤广告与冗余] │ │  ← ③ 元信息行（品牌色点+时长+徽标）
│ ├────────────────────────────────────────────┤ │
│ │  视频直链（N 条）            [复制全部] [JSON] │ │  ← ④ 直链区
│ │  提示：直链多带时效，播放走代理、复制得原文     │ │
│ │  ┌ 1080P 蓝光·AVC1/MP4A · 48.0 MB ────────┐ │ │
│ │  │ https://…（等宽字体，单击即复制）        │ │ │
│ │  │        [▶ 播放] [复制链接] [⬇ 下载]     │ │ │
│ │  └────────────────────────────────────────┘ │ │
│ └────────────────────────────────────────────┘ │
├────────────────────────────────────────────────┤
│ 仅供个人学习与备份自有内容 · 请遵守站点条款  v1.0  │  页脚：合规声明 + 版本号
└────────────────────────────────────────────────┘
```

### 2.2 三种工作模式（顶栏标签切换）

结果区「只有标题 + 播放器 + 直链」是硬约束，所以批量与定时**不塞进结果区**，
而是各自成为独立面板，由顶栏标签互斥切换：

```
┌──────────────────────────────────────────────────────────┐
│ ⛶ Video Lens   [单条解析][批量下载][定时任务]   ● 服务正常 │
├──────────────────────────────────────────────────────────┤
│ ① 单条解析：原来那四态（空态/加载/错误/结果）              │
│                                                          │
│ ② 批量下载：                                               │
│    ┌ 每行一条链接（也接受整段混排文本）──────────────┐    │
│    └ [批次名] 识别到 N 条 ············· [开始批量下载] ┘    │
│    ▓▓▓▓▓▓▓░░░░░ 已完成 3/5（成功 3）                      │
│    ● 视频标题一                      已完成 · 20.0 MB     │
│    ● 视频标题二                      合并中…              │
│    ● https://…                       失败 · 直链已过期    │
│    [下载 zip] [清空]        ▸ 历史批次                    │
│                                                          │
│ ③ 定时任务：                                               │
│    ┌ 要定时处理的链接 ─────────────────────────────┐      │
│    └ [备注] [每天定时▾][22:00] ······· [添加计划] ─┘      │
│    ┌ 每晚备份                                       ┐    │
│    │ 每天 22:00　下次：10-09 22:00                   │    │
│    │ 3 条链接：https://…                            │    │
│    │ 上次执行 10-08 22:00：完成 3 条       [立即执行] │    │
│    └                                    [暂停][删除] ┘    │
└──────────────────────────────────────────────────────────┘
```

**为什么用标签而不是并排**：三块要处理的是同一件事（拿到视频），并列会同时争夺注意力，
也违反"聚焦"的初衷。标签让每一刻只有一个明确的任务。

**模式切换的状态隔离**：`panels.js` 与 `app.js` 各管各的状态。切走时 `panels.js`
把单条解析的四个视图 `hidden`；切回时派发 `vl:mode-single` 事件，由 `app.js`
按自己记住的 `currentView` 恢复 —— 两边都不销毁对方的数据，切回来结果还在。

### 2.3 设计令牌与视觉规范（`css/style.css`）

- **暗色低饱和画布**：`--c-bg:#0d0f12` 页面底色 / `--c-bg-soft` 次级面 / `--c-bg-input` 输入底 / `--c-line` 全站唯一描边色，全部收敛为 `:root` CSS 变量；**唯一强调色** `--c-accent:#4c8dff` 只用于主按钮与当前态，语义色 `--c-ok/--c-warn/--c-err` 各司其职；
- 聚焦原则的视觉表达：**无侧栏、无卡片瀑布、无页脚导航**，整页只有「输入 / 标题+播放器 / 直链」四段；`--max-w:980px` 正文最大宽度接近 16:9 视频宽度，让画面成为页面唯一的亮面与彩色焦点；
- 文本层级只靠字号与灰阶（`--c-text/-2/-3`）区分，不用分隔线、阴影堆叠、背景色块划分区域；
- 「已过滤广告与冗余」徽标复用 `--c-ok` 绿（`#35c47f`），把后端过滤行为可视化；
- 直链用等宽字体（`--ff-mono`）呈现，强化"这是原始地址"的心智；复制成功以 `.is-copied` 绿色描边反馈；
- 批量条目用状态点（灰=等待 / 橙脉动=进行 / 绿=完成 / 红=失败）表达进度，不引入进度环或百分比数字堆叠；
- 响应式断点：≥1080 三栏可用宽度 / ≤880 单列 / ≤520 输入框纵向排布；并尊重 `prefers-reduced-motion`。

### 2.4 前端开发指南（`js/app.js`）

**状态机**：`empty | loading | result | error` 四态互斥（`show(name)` 统一切换 `hidden` 并写入 `body.dataset.view`），任何 API 响应只做一次状态迁移，不存在"加载与结果同屏"的中间态。

> ⚠️ 两条互斥保障，改动时勿删：
> 1. `css/style.css` 里有全局兜底 `[hidden] { display: none !important; }`。本文件多处规则写了 `display: flex`（`.vl-empty`/`.vl-loading`/`.vl-error`/`.vl-result`…），作者级 `display` 会盖掉浏览器 UA 的 `[hidden]{display:none}`，缺了这行四个视图会**同时显示**（表现为结果下方多出一块"多余"的错误面板）。
> 2. `app.js` 用 `state.gen`（提取代际号）给每一次提取打标，`startExtract` 自增，`poll` 在 `await` 前捕获、`await` 后重新核验 `gen`/`jobId`/`searching`；旧任务被新提取顶替后，它的迟到响应（无论 `done` 还是 `failed`）一律丢弃，不会把新结果刷成错误态。

**任务轮询**：`POST /api/extract` 拿到 `{job_id, expected_ms}` 后每 700ms 轮询 `GET /api/jobs/{id}`；进度条按 `expected_ms` 映射时间轴（不撒谎的进度）；「取消」只本地停止轮询，不通知后端（后台线程自行收尾），并回到上次结果或空态。

**渲染纪律**：DOM 一律 `getElementById` + `<template>.cloneNode`（`tpl-link-item` / `tpl-quality-item` 两个模板），站点数据全部走 `textContent`，杜绝 XSS 注入口。

**播放器**：默认源取 `result.default_index`；`play_url` 是同源代理相对路径；HLS 源走 hls.js（Safari 用原生）；切清晰度 = `setSource(i, {autoplay:true})`；`video.onerror` 时在播放器上给出「直链已失效，请重新提取」的显式提示，而不是留一个转圈的黑屏。

**联调约定**：`direct_url`（原文直链，展示/复制/下载用）与 `play_url`（代理地址，本页播放用）永远成对出现；前端不接触站点请求头（Referer/UA 由代理层 `HeaderJar` 暂存）。

> 校验工具：`python scripts/check_frontend.py` 检查 JS 引用与 HTML id/class/模板的对齐（当前 42/42 全对齐）。

---

## 三、后端解析逻辑规划

### 3.1 请求全链路

```
浏览器                FastAPI                      视频站
  │  POST /api/extract   │
  ├─────────────────────▶│ 限流 → check_url(SSRF) → jobs.create
  │   202 {job_id,expected_ms}                      │
  │◀─────────────────────┤                           │
  │  每700ms GET /api/jobs/{id}                     │ _supervise 看门狗(45s)
  ├─────────────────────▶│      └─ to_thread ─▶ extract(url)
  │◀─────────────────────┤         yt-dlp.extract_info ───────▶ 站点API
  │  {status,step,…}     │         ◀── formats[100+] 原始流 ──── 站点CDN
  │                      │         _pick_sources：广告✂ DRM✂ 单轨✂ 去重✂ 截断✂
  │                      │         白名单投影 → jobs(DONE)
  │  GET /api/stream?u&t  │
  ├─────────────────────▶│ HMAC 验签 → HeaderJar 取回站点请求头
  │                      │ ├ m3u8：取清单→逐行改写资源地址→返回
  │  200/206 字节流        │ └ 其他：Range 透传 + aiter_raw 流式转发
  │◀═════════════════════┤   （HEAD 探元信息分支同样支持）
```

### 3.2 模块职责

| 模块 | 职责 | 关键设计 |
|------|------|---------|
| `main.py` | 路由装配 | 15 个业务端点：解析（`/`、`/api/health`、`/api/extract`、`/api/jobs/{id}`、`/api/stream`）、合并下载（`POST /api/remux`、`GET /api/remux/{id}/file`）、批量（`POST /api/batch`、`GET /api/batch/{id}`、`GET /api/batch/{id}/file`、`DELETE /api/batch/{id}`、`GET /api/batches`）、定时（`/api/schedules` ×3 + `POST /api/schedules/{id}/run` + `DELETE`）；`/api/*` 全部先声明，`StaticFiles` 最后挂载（顺序不可调换）；`/api/stream`、`/api/health`、`/` 显式声明 `methods=["GET","HEAD"]`（本版本 FastAPI 的 APIRoute 不给 GET 自动加 HEAD，而播放器取流前普遍先 HEAD 一次，缺它会 404） |
| `extractor.py` | 解析+过滤 | 白名单投影（1.2 节）；`_YdlLogger` 捞回被 `ignoreerrors` 吞掉的根因；`classify_error` 把站点英文报错翻译成 9 类「错误码+下一步建议」；`_title_of` 对裸 m3u8 直链做标题兜底（避免把 URL 哈希当标题显示） |
| `jobs.py` | 异步任务 | 内存 LRU(256)+TTL(1h)；状态机 `queued→running→done/failed`；DONE 写入前有 `status==RUNNING` 守卫，防看门狗判超时后状态回跳 |
| `security.py` | 输入防线 | 协议/长度/控制字符/userinfo/IP 五道校验；HMAC-SHA256 对 `exp|url` 签名，令牌不可续期、不可改指向 |
| `stream_proxy.py` | 播放通路 | 为什么必须代理见 `extractor.proxy_path` 注释；`HeaderJar` LRU(512) 暂存采集头不进 URL；清单改写把子清单/密钥/分片全部指回代理 |
| `scraper.py` | 页面兜底 | yt-dlp 抛 `Unsupported URL` 时退一步只读抓页面：`og:title`→`twitter:title`→`<h1>`→`<title>` 取标题；正则扫出 m3u8/mp4 直链（能吃 JSON 双重转义 `https:\\/\\/…`），按「主播放器上下文 > 广告位」排序 |
| `remux.py` | HLS 合并下载 | 复用 `jobs` 状态机（文案换成「合并」）跑 ffmpeg `-c copy` 把 m3u8 分片存成本机 MP4；产物只登记在内存、不进任务结果；**按已写字节上报百分比进度**；`sweep` 回收孤儿文件；无 ffmpeg 时抛 `ffmpeg_missing` |
| `playlist.py` | 清单分析 | 合并前算「码率 × 时长」得预估体积，并给出总时长作为进度分母；实测误差 ~3%；拿不到就返回 None，绝不编数字 |
| `batch.py` | 批量下载 | 逐条提取（受信号量限流）+ 失败隔离 + 打成一个 zip；`pack_state` 区分"条目跑完"与"打包完成"；非 HLS 直链延迟到打包时才拉 |
| `scheduler.py` | 定时调度 | `daily` / `interval` 两种模式；触发即重算并落盘，避免重复执行；错过超宽限期则跳过并记录原因；单个计划失败不影响其它 |
| `store.py` | 持久化 | JSON + 内存镜像；原子写入（临时文件 → fsync → `os.replace`）；损坏自动备份为 `.broken` 后当空库启动；只存 URL/时间/状态，直链不落盘 |

### 3.3 七条"为什么"

- **为什么任务化而不是同步？** 解析常需 10~30s，同步长请求会被浏览器/代理层超时截断且无进度；202+轮询换来可取消、可显示步骤、可看门狗兜底。
- **为什么必须反代直链？** 站点 CDN 无 CORS 头、校验 Referer/UA（浏览器禁止 JS 设置）、直链常为 http（https 页面混合内容被拦）——三关全死，只有服务端回源可解。
- **为什么限制直链可访问性（HMAC）？** 否则本服务退化为任意 URL 转发器（open proxy），会被拿去打内网。令牌绑定「exp+规范化 URL」，过期站点签名与令牌过期双重失效都会引导前端「重新提取」。
- **为什么 m3u8 的「下载」要走服务端合并而不是直接存清单？** m3u8 只是分片清单（纯文本），直接存盘在脱离本服务后就是**打不开的废文件**（改写版分片还指回 `/api/stream`）。故当本机有 ffmpeg 时，「下载」= 服务端 `remux.py` 逐片取流并 `-c copy` 封装成 MP4；没有 ffmpeg 才降级为存**原始未改写**清单（分片仍是相对地址，需联网，可把直链交给 VLC/mpv 网络串流）。
- **为什么"yt-dlp 不支持"还要再兜一层？** "没有提取器" ≠ "取不到"。大量中小站把播放地址明文写在 HTML/JS 里，yt-dlp 只会抛 `Unsupported URL` 就放弃，而用户真正想要的是**标题 + 能播的直链**。`scraper.py` 因此只读抓一次页面，把标题与直链捞出来复用同一条投影链路。它是纯增量：抓不到就照旧报原来的错，行为与从前一致。
- **为什么批量要打成 zip 而不是逐个下载？** 浏览器一次只弹一个保存框，连发 N 个下载只会被拦或被用户点掉一堆。一个 zip 才是"批量下载"的正确交付形态。代价是必须等所有条目完成，所以它天然是个有终态的任务（也因此需要 `pack_state` 区分"条目跑完"与"打包完成"）。
- **为什么定时状态要落盘而不是放内存？** 定时的全部意义就是"提交后不用盯着"。若计划只活在内存里，服务一重启计划就没了 —— 那这个功能等于不存在。所以 `store.py` 用原子写入把状态持久化到 `backend/state.json`，并且损坏时备份而不是静默丢弃。

---

## 四、核心代码

完整实现见仓库文件，此处摘最关键的两段（其余模块的逐行说明在源文件 docstring 内）。

### 4.1 后端 · 白名单投影与流过滤（`app/extractor.py`）

```python
def _pick_sources(raw_formats):
    """流过滤 → 去重 → 排序 → 截断，输出前端可直接渲染的 sources 列表。"""
    candidates, dropped = [], {"ad": 0, "drm": 0, "unplayable": 0, "empty": 0}
    for fmt in raw_formats:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            dropped["empty"] += 1; continue
        if _looks_ad(fmt):      dropped["ad"] += 1; continue   # ① 贴片/广告流
        if _has_drm(fmt):       dropped["drm"] += 1; continue  # ② DRM 加密流
        if not _is_playable(fmt):  dropped["unplayable"] += 1; continue  # ③ 单轨/不支持协议
        candidates.append(fmt)

    # ④ 同一 (高度, 视频编码, 封装) 只留码率最高的一条
    best = {}
    for fmt in candidates:
        key = (_height_of(fmt), (fmt.get("vcodec") or "none").split(".")[0], _container_of(fmt))
        cur = best.get(key)
        if cur is None or _bitrate_of(fmt) > _bitrate_of(cur):
            best[key] = fmt

    # ⑤ 按画质排序，最多展示 6 条
    ordered = sorted(best.values(),
                     key=lambda f: (_height_of(f), _bitrate_of(f), f.get("fps") or 0),
                     reverse=True)[:_MAX_SOURCES]
    ...  # 逐条组装 label/尺寸/直链/代理地址（见源文件）

def extract(url, progress=None):
    ...
    # —— 白名单投影：UI 之外的字段到此为止，绝不外泄 ——
    return {
        "title": (entry.get("title") or "未命名视频").strip()[:200],
        "provider": provider, "provider_color": color,
        "duration": int(duration) if duration else None,
        "thumbnail": _pick_thumbnail(entry), "is_live": bool(entry.get("is_live")),
        "sources": sources, "default_index": _default_index(sources),
        "filtered": {"removed_ads": True, "max_sources": _MAX_SOURCES},
    }
```

### 4.2 后端 · 播放代理的 HLS 清单改写（`app/stream_proxy.py`）

```python
def rewrite_manifest(text, base_url):
    """把 m3u8 里的相对/绝对资源地址全部指向本机代理。

    规则：
      - ``#`` 开头的标签行 → 只就地替换其中的 URI="..."（覆盖 EXT-X-KEY / EXT-X-MAP），
        没有 URI 属性的标签（EXTINF 等）原样保留，播放器解析依赖它们；
      - 非注释行即资源地址（分片或子清单），urljoin 成绝对 URL 后换成代理路径。
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line); continue
        if stripped.startswith("#"):
            out.append(_rewrite_tag_uri(stripped, base_url)); continue
        out.append(_proxy_for(_abs(stripped, base_url), base_url))
    return "\n".join(out) + "\n"

def _rewrite_tag_uri(line, base_url):
    """改写标签内 URI="..." 的取值（只处理第一个 URI= 属性，够覆盖 KEY/MAP）。"""
    match = re.search(r'URI="([^"]+)"', line)
    if not match:
        return line
    inner = match.group(1)
    if inner.startswith("data:") or inner.startswith("sk-"):
        return line                      # 内联/程序化密钥无需回源
    return line[:match.start(1)] + _proxy_for(_abs(inner, base_url), base_url) + line[match.end(1):]
```

> 为什么分片也要过代理：以 `/` 开头的地址会被播放器按「清单自身的 origin」解析，正好回到本机 `/api/stream`，于是每个分片请求都由服务端补上 Referer/UA —— 这是 HLS 能在本页播放的根因。
> 例外：`dl=1` 下载模式返回**未改写**的原始清单，因为代理清单只在服务存活期间有效，存盘后即废文件。

### 4.2b 后端 · HLS 合并下载（`app/remux.py`）

要解决的痛点：**m3u8 能预览、下载却存成一个打不开的文本文件。** 清单不含画面，
预览时是 hls.js 逐片取流；直接存盘得到的是清单本身。改造后的下载链路：

```python
cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
if head := _headers_arg(url):            # 从 HeaderJar 取回提取时的 Referer/UA
    cmd += ["-headers", head]            # 多行以 CRLF 分隔，否则 B站/抖音类源站回 403
cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
       "-i", url,                        # ★ 原始 direct_url，不是改写后的代理清单
       "-c", "copy",                     # 只转封装不重编码：耗时≈下载一遍
       "-f", "mp4", "-movflags", "+faststart", "-y", path]
```

四条不可省略的约束：

1. **喂原始直链**（`sources[i].direct_url`）。代理清单里的分片是 `/api/stream?u=…` 相对地址，
   ffmpeg 在本机按 `file://` 基址解析，只会全部找不到文件；分片相对地址与 `#EXT-X-KEY`
   密钥 URI 由 ffmpeg 按远端 base 自行解析，master playlist 自动挑最高变体。
2. **产物路径不出后端**。任务结果只有 `{kind, mp4, size_bytes, filename}`，
   绝对路径留在 `remux._outputs` 内存表里；否则等于对外暴露服务器文件系统。
3. **看门狗用 `REMUX_TIMEOUT`（默认 900s）**，不能复用解析的 45s，否则稍长的视频必被误杀；
   `_spawn` 内另有体积上限（`REMUX_MAX_BYTES`）与逐 0.5s 的超时中止，`terminate` 后再 `kill`。
4. **清理归本模块**。`jobs` 的 LRU 淘汰不会回调，故 `sweep()` 在每次 `start()` 时回收孤儿产物；
   下载接口不即删，TTL 内可重复取（浏览器下载中断后还能再点一次）。

失败文案由 `_explain()` 翻译：403/404 → 「直链多半已过期，请回到本页重新提取」；
`EXT-X-KEY`/decrypt → 「带加密，服务端没有密钥」；codec/muxer → 「该编码不适合直接封装进 MP4」。
前端只改一个按钮的文案与 `#toast`（idle「下载 MP4」→ busy「合并中 mm:ss」→ ready「保存 MP4」），
不新增任何面板，符合「界面只有标题 + 播放器 + 直链」的硬约束。


### 4.2c 后端 · 批量下载（`app/batch.py`）

要解决的痛点：**一次只想粘一堆链接，最后拿到一堆文件。**
浏览器一次只弹一个保存框（多发几个只会被拦或被用户点掉），所以批量的正确交付形态
是**一个 zip**；而 zip 要求所有条目都完成，因此它必须是个有终态的任务。

```python
# 1) 从混排文本里挑链接 —— 用户常从聊天记录整段粘贴，混着中文说明与序号
_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]，。；]+", re.I)

# 2) 逐条处理（BATCH_CONCURRENCY 限流），失败隔离：一条坏不影响其余
await asyncio.gather(*(one(it.item_id, it.url) for it in batch.items))

# 3) 全部落定后才打包；此时才真正拉取非 HLS 直链（延迟下载，不白耗流量）
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
    ...
```

四个关键决策：

1. **坏链接不拒绝整批**。用户整段粘贴时混进一两条失效/内网地址很正常，整批 400 会逼他
   逐条排查。改为把不合格的挑出来**单独标 failed 并写明原因**，其余照常跑。
   （与站点无关的 SSRF 校验仍然照做，只是不再"一颗老鼠屎坏一锅汤"。）
2. **打包阶段是独立状态位**（`pack_state: idle → packing → packed`）。条目全 DONE 到
   zip 落盘之间有个窗口期；若此时就报 finished，前端弹出的下载按钮点下去只能拿到 409。
3. **非 HLS 直链在打包时才拉**：只在确定要交付时才耗流量，且边下边写入 zip，
   不落临时文件（省一倍磁盘）。
4. **限流按"条"计**：一次提交 20 条对外就是 20 次站点请求，按"次"限流等于开了个口子。

### 4.2d 后端 · 定时任务与持久化（`app/scheduler.py` · `app/store.py`）

**调度语义（刻意保持可预测）**

```python
def compute_next(sched, now=None):
    if sched.mode == "interval":
        return now + max(1, sched.every_minutes) * 60     # 简单可预测，不对齐整点
    hh, mm = _parse_at(sched.at)                          # daily："HH:MM"
    target = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0)
    return (target + timedelta(days=1) if target <= now else target).timestamp()
```

- 每次 tick 扫描启用的计划，`next_run_at` 到点即触发；
- **触发后立刻算下一次并落盘**，所以进程在两次触发之间被杀也不会重复跑；
- **触发瞬间就写入 `last_run_at` / `last_batch` / `running` 状态**。一次执行可能几十分钟
  （HLS 要逐条合并），若等到结束才更新，这期间界面上完全看不出"正在跑"，
  用户会以为定时没生效（这是实际反馈过的问题）；执行结束再用真实时刻重算 `next_run_at`；
- **启动时收拾半路夭折的执行**：进程被强杀时 `finally` 不会执行，状态会永久停在
  `running`（界面上像"卡住了"）。`reconcile_stale_runs()` 在启动时把它们落成"已中断"；
- 停机期间错过的计划：宽限期（`SCHEDULE_MISSED_GRACE`，默认 1 小时）内**补跑一次**
  （合盖、临时重启这类场景应该补上），超期则跳过并记录原因 ——
  否则开机时会把过去一周的每日任务连着跑十遍；
- 改计划必须**重算下次时间**，否则会出现"改成 22:00 但下次仍按旧的 08:00 跑"这种
  让人无法信任定时功能的诡异行为。

> **界面上怎么判断定时到底跑没跑**：计划行会显示 `● 正在执行中…`（橙色，边框高亮）
> 与已写入的开始时间；执行完变成「完成 N 条（批次 xxx）」。因为一次执行可能要很久，
> 定时面板每 5 秒自动刷新（全部静止后退回 20 秒一次），不需要手动刷页。

**为什么不用 cron / APScheduler**

本项目的定时需求很窄（每天某时刻 / 每隔 N 分钟）。为此引入 cron 解析库属于给一个
二十行的循环加一整套依赖；更关键的是**计划要活过重启**，而这需要与 `store` 的状态整合，
外部调度器反而更难做一致。

**持久化（`store.py`）的三个要点**

1. **原子写入**：先写临时文件 → `fsync` → `os.replace`。直接覆写时若进程被杀会留下
   半截 JSON，下次启动整个文件读不出来 —— 那意味着所有计划一起丢失。
2. **损坏即备份**：读到无法解析或 schema 不符的文件时，备份成 `.broken` 再当空库启动，
   绝不静默吃掉用户的计划。
3. **不存敏感信息**：只存 URL、时间、状态。直链（带站点签名）不落盘 ——
   它们本来就有时效，存下来既没用又多一处泄露面。

### 4.2e 后端 · 体积预估与合并进度（`app/playlist.py`）

要解决的两个问题：**点下载之前不知道多大，点下去之后不知道还要等多久。**
两件事都缺一个"分母"：体积要「码率 × 时长」，进度要「总时长」。

```python
# 能拿到什么（都来自清单本身，不需要预下载分片）
#EXT-X-STREAM-INF:...AVERAGE-BANDWIDTH=2168183   → 码率（站点声明，优先用它）
#EXTINF:10.000,                                   → 逐行累加得精确总时长
分片 Content-Length                              → 最准，但要逐个 HEAD
                                                  只在没有 BANDWIDTH 时抽样外推

估算：大小 ≈ 码率(bps) × 时长(s) / 8      # -c copy 不重编码，所以几乎等于产物大小
```

**实测精度**（`test-streams.mux.dev` 720p 档）：预估 **19.6 MB** vs 实际 **19.1 MB**，
误差 **2.6%**。分片抽样外推的误差实测 3.6%。

四个关键决策：

1. **必须分析"即将被 ffmpeg 读取的那一条 URL"**，不能拿主清单去猜。
   踩过的坑：主清单最高档 6.2 Mbps（预估 471 MB），而 yt-dlp 给我们的 `direct_url`
   其实是 246 kbps 那档（产物 19.1 MB）—— 差 25 倍，界面会写"下载 MP4（470 MB）"。
   所以 `analyze(url, follow_master=False)` 是**本项目唯一正确的调用口径**。
2. **估算值与确定值分字段**（`size_bytes` vs `size_estimate_bytes`）。前端用「约 20 MB」
   与虚线下划线标记估算，绝不把估算冒充成精确值。
3. **进度用「已写字节 / 预估字节」**，而不是 ffmpeg 的 `-progress` 输出。
   ffmpeg 边下分片边写 mp4，文件体积就是真实进度；`-progress` 要多解析一路管道，
   多一路读线程就多一处可能卡死。
4. **百分比封顶 99%**。最后还有 moov 收尾与落盘，没到 DONE 就不该显示 100%，
   否则进度条先满、再等几秒才完成，观感上像卡住。DONE 时由后端强制归 100%。
5. **拿不到分母时不编百分比**：返回 `progress: null`，前端画不确定态滚动条 +
   只报「已写入 X MB · 已用 Ys」，明确表示"进度未知"而不是"卡住了"。

> 前端体验：直链行的下载按钮直接写「下载 MP4（约 20 MB）」；点下去后按钮变
> 「合并中 48%」，行内出现一条进度条，文案是「9.5 MB / 约 19.6 MB · 已用 00:12」。
> 批量面板里每个条目也有自己的细进度条（整批进度条看不出卡在哪一条）。

### 4.3 前端 · 结果渲染与播放切换（`js/app.js` 摘录）
```js
function render(data) {                       // 只渲染白名单字段，界面无任何站点冗余信息
  state.data = data;
  show("result");                             // 四态互斥：empty | loading | result | error
  el.title.textContent = data.title || "未命名视频";   // ① 标题
  el.adBadge.hidden = !data.filtered?.removed_ads;    // 「已过滤广告与冗余」徽标
  renderSources(data.sources || []);          // ④ 直链列表 + 清晰度浮层（template.cloneNode，站点数据全走 textContent）
  el.duration.textContent = data.is_live ? "直播中" : fmtDuration(data.duration);  // ③ 元信息行
  const idx = pickDefault(data);              // ② 播放器：默认清晰度
  if (idx >= 0) setSource(idx, { autoplay: false });
}

function setSource(index, opts) {
  const src = state.data.sources[index];
  stopHls();
  const abs = (u) => new URL(u, location.href).href;   // play_url 是同源相对路径
  if (src.is_hls) {
    const nativeHls = el.video.canPlayType("application/vnd.apple.mpegurl") !== "";
    if (nativeHls) el.video.src = abs(src.play_url);   // Safari 原生播 HLS
    else {                                   // 其余浏览器：hls.js 装载已被后端改写好的代理清单
      const hls = new window.Hls({ maxBufferLength: 30, enableWorker: true });
      hls.loadSource(abs(src.play_url));     // 分片地址已指向 /api/stream，无需 CORS
      hls.attachMedia(el.video);
      state.hls = hls;
    }
  } else {
    el.video.src = abs(src.play_url);        // mp4/webm：Range 透传，进度条随便拖
  }
  el.video.onerror = () => el.note.textContent =
    "播放失败：代理直链已失效，请点击「重新提取」获取新直链";   // 过期引导
}
```


---

## 五、部署与运行使用说明

### 5.1 快速启动（开发/本机）

```bash
# 1) 环境：Python ≥ 3.10
python -m pip install -r backend/requirements.txt

# 2) 启动（默认 127.0.0.1:8300）
python backend/run.py
#    开发热重载： python backend/run.py --reload
#    换端口：    python backend/run.py --port 8399   （或环境变量 VX_PORT）

# 3) 打开浏览器 → http://127.0.0.1:8300
#    API 文档 → http://127.0.0.1:8300/api/docs
```

Windows PowerShell 等价命令：`$env:VX_PORT="8399"; python backend\run.py`。

### 5.1b 容器部署（NAS / 飞牛 OS 推荐）

镜像内置 Python 与 ffmpeg，不依赖宿主环境，适合飞牛 OS、群晖这类 NAS 长期挂机。

```bash
cd video-extract
# 改 docker-compose.yml 里的两处：VX_SECRET（openssl rand -hex 32）与卷路径
docker compose up -d --build
docker compose logs -f          # 启动日志会打印密钥/目录自检结果
```

访问 `http://<NAS的IP>:8300`。

**镜像的四个设计点**（见 `Dockerfile` 注释）：

| 决策 | 原因 |
|------|------|
| ffmpeg 内置 | 缺它 HLS 只能降级成"下载一份打不开的清单"，而 NAS 上用户很难自己补装 |
| 非 root（uid 1000） | 容器不需要特权；写脏宿主目录后很难收拾。uid 1000 正好对齐飞牛默认管理员 |
| 多阶段构建 | gcc 等编译工具只留在 builder 层，运行层精简（NAS 拉镜像慢） |
| 数据全在 `/data` 卷 | `state.json`（定时计划）与合并产物都在这里，重建容器不丢计划 |

**健康检查**走独立脚本 `docker/healthcheck.py`（打 `/api/health`，顺带验证 ffmpeg 能力位），
而不是内联的 `python -c`：后者要叠 Dockerfile 续行、shell 引号、Python 语句三层转义，
任何一层写错都会变成"容器永远 unhealthy"这种很难查的故障。

> 飞牛 OS 的完整部署步骤、目录权限处理与常见问题，见 **[DEPLOY-fnOS.md](DEPLOY-fnOS.md)**。

### 5.1c 验证镜像目录结构（无需 Docker）

若本机没装 Docker，仍可验证"按镜像结构部署"不会出问题：

```bash
python -X utf8 scripts/sim_image_layout.py
```

它会把项目按 `app/backend` + `app/frontend` + `data/` 的结构复制到临时目录，
用容器同款环境变量起服务，然后真实访问首页/静态资源/API 并检查状态文件落在数据卷里。
**这一步能抓出"本机路径恰好对、容器里 path 算错"这类只在部署后才暴露的问题。**

### 5.2 生产部署

```bash
# 对外监听 + 固定令牌密钥（不设则每次重启随机生成，旧播放链接集体失效——属预期防泄漏设计）
cd backend
export VX_HOST=0.0.0.0
export VX_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export VX_RATE_LIMIT_RPM=60

python -m uvicorn app.main:app --host "$VX_HOST" --port "${VX_PORT:-8300}" \
       --proxy-headers --workers 1   # 任务表在进程内存中，多 worker 需粘性路由，建议单 worker
```

前端与 API 同源托管（`StaticFiles`），生产**无需**额外 CORS；仅当前端单独部署时才设 `VX_ALLOW_ORIGIN=https://你的前端域名`。

### 5.3 环境变量总表（`VX_` 前缀）

| 变量 | 默认 | 说明 |
|------|------|------|
| `VX_HOST` / `VX_PORT` | `127.0.0.1` / `8300` | 监听地址/端口 |
| `VX_SECRET` | 随机 | 代理令牌 HMAC 密钥，**生产必设** |
| `VX_TOKEN_TTL` | 21600 | 代理链接有效期（秒） |
| `VX_RATE_LIMIT_RPM` | 30 | 每 IP 每分钟提取次数 |
| `VX_EXTRACT_TIMEOUT` | 45 | 单任务解析看门狗（秒） |
| `VX_BLOCK_PRIVATE_NET` | true | SSRF：禁解析到内网/回环/链路本地 |
| `VX_IMPERSONATE` | 空 | 如 `chrome`：启用 curl_cffi TLS 指纹（`pip install curl_cffi`），解风控型站点 |
| `VX_COOKIES_FILE` | 空 | Netscape `cookies.txt` 路径：B站大会员 1080P/4K、登录可见内容 |
| `VX_ALLOW_ORIGIN` | 空 | 仅前后端分离部署时设置 |
| `VX_LOG_LEVEL` | INFO | 日志级别 |
| `VX_FFMPEG_PATH` | `ffmpeg` | ffmpeg 可执行文件名或绝对路径（PATH 找不到时还会探测 winget/常见安装目录） |
| `VX_REMUX_TIMEOUT` | 900 | 单次 HLS 合并最长秒数（≈把视频下载一遍的耗时上限） |
| `VX_REMUX_CONCURRENCY` | 2 | 同时进行的合并任务数，通道占满会明确提示「请稍后重试」 |
| `VX_REMUX_MAX_BYTES` | 2GiB | 合并产物体积上限，超限立即中止并提示改用本地工具 |
| `VX_REMUX_TEMP_DIR` | `backend/.remux_tmp` | 合并产物临时目录（取走后由 TTL 清扫回收） |
| `VX_SCRAPE_FALLBACK` | true | yt-dlp 无提取器时是否读页面兜底（关掉即恢复「不支持就报错」） |
| `VX_SCRAPE_MAX_BYTES` | 2MiB | 兜底抓取的页面体积上限，防止超大页面吃内存 |
| `VX_BATCH_MAX_URLS` | 20 | 单批最多链接数 |
| `VX_BATCH_CONCURRENCY` | 2 | 同时处理的条目数（太高会被源站风控、也会打满本机） |
| `VX_BATCH_TIMEOUT` | 3600 | 整批看门狗（秒） |
| `VX_BATCH_ZIP_MAX_BYTES` | 8GiB | 打包产物体积上限，超限丢弃并说明 |
| `VX_SCHEDULE_ENABLED` | true | 关掉即停用定时调度（已存计划保留，只是不触发） |
| `VX_SCHEDULE_TICK` | 20 | 调度器轮询间隔（秒） |
| `VX_SCHEDULE_MISSED_GRACE` | 3600 | 停机错过的计划在多久内仍补跑（秒），超期则跳过 |
| `VX_STORE_FILE` | `backend/state.json` | 计划与批次状态的落盘位置 |

**没有 ffmpeg 会怎样？** 功能自动降级，不影响其它一切：`/api/health` 的 `ffmpeg_available` 为
`false`，m3u8 的「下载」退回存**未改写**的原始清单；清单里分片是相对地址，存盘后仍需联网，
正确用法是把该直链交给 VLC / mpv 的「网络串流」——按钮 `title` 里已写明这一点。
Windows 安装：`winget install Gyan.FFmpeg`（装完重启服务即自动识别）。

### 5.4 冒烟测试与验收

```bash
# 先按 5.1 启动服务，然后：
python -X utf8 scripts/smoke.py http://127.0.0.1:8300
# 覆盖 85 项断言：健康检查/静态资源/SSRF 拒绝/提取→轮询→完成、
# 字段白名单结构性断言（冗余字段出现即失败）、代理取流/Range 206/HEAD 探活/
# 下载 attachment/伪造令牌 403/非法编码 400/HLS 清单改写单测、
# 裸 m3u8 标题兜底 + 服务端合并出真 MP4（校验 ftyp 魔数、可重复下载）、
# 页面兜底解析（标题来源优先级/多视频不折叠/JSON 双重转义直链/广告位排序）、
# 批量下载（链接识别去重、坏链接不拒整批、zip 合法且只含成功条目）、
# 定时任务（建计划→算下次时间→启停重算→删除、触发后立刻可见 running→终态→重算）、
# 体积预估与进度（预估码率与源自身码率吻合、进度单调不减且不超 99、完成归 100）、
# 状态文件 schema

# 前端静态一致性（DOM id/class/模板对齐 + 按钮状态机断言）
python -X utf8 scripts/check_frontend.py

# 无头浏览器 UI 回归：真点一遍模式切换与批量提交（需本机有 Chrome/Edge）
node scripts/ui_check.mjs
```

### 5.5 使用要点与已知边界

- **直链有时效**：站点签名过期后播放/下载会得到 403/502，界面与接口文案均引导「重新提取」，这是正确行为；
- **需要登录的清晰度**：配置 `VX_COOKIES_FILE`（浏览器登录站点后用扩展导出 cookies.txt）；
- **风控站点**：设置 `VX_IMPERSONATE=chrome` 并安装 curl_cffi，`/api/health` 的 `impersonate_available` 可确认；
- **YouTube 免费版清晰度**：未装 curl_cffi 时通常返回 360P 合并流（1 条源），装好指纹后可得更高分辨率；
- **B站个别稿件解析失败**：属 yt-dlp 上游提取器问题（如个别 BV 号触发 `KeyError('bvid')`），服务会如实展示上游错误原文而不是静默"无响应"；升级 yt-dlp 通常可解；
- **重启服务**：所有旧的 `play_url` 签名作废（未固定 `VX_SECRET` 时必然如此），页面重新提取即可。
- **裸 m3u8 直链没有标题**：直接粘一个 `.m3u8`（或 CDN 上无扩展名的 HLS 地址）时，上游只会拿 URL 文件名当标题，那是一串哈希而不是标题。此时界面显示「未命名视频（m3u8 直链不含标题信息）」，不再把哈希伪装成标题；想拿到真实标题请粘**视频页链接**，由解析器从页面读取。
- **合并下载的边界**：带 `EXT-X-KEY` 加密的源服务端没有密钥，会明确失败；`-c copy` 只转封装，个别编码（如某些 fMP4 变体）可能只封装到首个分片，得到的是短但真实的文件；超过 `VX_REMUX_MAX_BYTES` / `VX_REMUX_TIMEOUT` 会中止并提示改用 yt-dlp/VLC 本地下载。合并产物在服务重启或 TTL 到期后回收，届时按「已过期」提示重新发起即可。
- **批量下载的边界**：一次最多 `VX_BATCH_MAX_URLS`（默认 20）条；条目按 `VX_BATCH_CONCURRENCY`（默认 2）并发，所以 20 条不会瞬间打出 20 个外呼；**一条失败不影响其余**，zip 里只放成功的，失败原因逐条显示；全失败时不产出 zip（提示"没有可打包的内容"）。打包产物在 TTL 到期后回收，历史批次记录会保留但下载按钮会提示重新提交。
- **定时任务的边界**：时间表达只有「每天 HH:MM」与「每隔 N 分钟」两种。服务停机期间错过的计划，在 `VX_SCHEDULE_MISSED_GRACE`（默认 1 小时）内会补跑一次，超期则跳过并记录原因（避免开机时连跑一周的积压任务）。定时执行的结果同样进入「历史批次」，可在批量面板查看与下载。关闭调度用 `VX_SCHEDULE_ENABLED=false`（已保存的计划会保留，只是不再触发）。
- **状态文件**：`backend/state.json` 保存计划与批次记录（原子写入，重启不丢）。它不含任何直链或 Cookie；如被外部工具改坏，启动时会备份成 `state.json.broken` 并以空库启动，不会静默丢数据。
- **体积是估算，不是承诺**：HLS 源的「约 xx MB」由「声明码率 × 总时长」算出，实测误差约 3%。它随清单里的 `BANDWIDTH` 走，站点若虚标就会偏；非 HLS（mp4 直链）的体积来自站点响应头，是精确值，因此不加「约」字、不打虚线标记。多清晰度列表里**每一档各有自己的预估**，看的是你当前选中那一档。
- **合并进度**：分母是预估值，所以进度到 99% 后可能还要等几秒（moov 收尾 + 落盘），这是正常的；拿不到清单时长/码率的源会显示不确定态滚动条而不是假百分比。
- **容器部署的权限**：容器以 uid 1000 运行，宿主挂载目录必须是 1000:1000（`chown -R 1000:1000 <目录>`）。忘了这一步时服务**不会崩**，但启动日志会明确报出「目录不可写」并给出这条命令 —— 因为等到点下载才失败会难查得多。
- **VX_SECRET 用模板占位值 = 没有防护**：占位值是公开的，任何人拿它都能伪造合法的播放/下载令牌。启动时会打 ERROR 提醒（生成用 `openssl rand -hex 32`）。
