"""端到端冒烟测试：提取 → 代理播放 → Range → 下载 → 防盗链 → 非法令牌 → HLS 合并。

用法：python -X utf8 scripts/smoke.py [base_url]
（合并用例需要本机装有 ffmpeg；没有时自动跳过并提示降级行为）
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8399"
TARGET = "https://www.w3schools.com/html/mov_bbb.mp4"

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}  {extra}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")


def main() -> int:
    c = httpx.Client(timeout=60, follow_redirects=True)

    print("== /api/health ==")
    r = c.get(f"{BASE}/api/health")
    check("health 200", r.status_code == 200, r.text[:90])

    print("== 静态资源 ==")
    for path in ("/", "/css/style.css", "/js/app.js", "/vendor/hls.min.js"):
        r = c.get(BASE + path)
        check(f"{path} 200", r.status_code == 200, f"{len(r.content)}B")

    print("== SSRF / 参数校验 ==")
    r = c.post(f"{BASE}/api/extract", json={"url": "http://127.0.0.1/admin"})
    check("内网地址被拒 400", r.status_code == 400, r.text[:70])
    r = c.post(f"{BASE}/api/extract", json={"url": "ftp://example.com/a.mp4"})
    check("非 http 协议被拒 400", r.status_code == 400, r.text[:70])

    print("== 提取任务 ==")
    r = c.post(f"{BASE}/api/extract", json={"url": TARGET})
    check("POST /api/extract 202", r.status_code == 202, r.text[:80])
    job = r.json()["job_id"]

    state = None
    for _ in range(30):
        time.sleep(1.5)
        state = c.get(f"{BASE}/api/jobs/{job}").json()
        if state["status"] in ("done", "failed"):
            break
    check("任务完成", state and state["status"] == "done", json.dumps(state and state.get("error"), ensure_ascii=False)[:120])
    if state["status"] != "done":
        return 1

    res = state["result"]
    check("字段白名单：无 description/uploader/uploader_id/view_count/like_count/tags",
          not ({"description", "uploader", "uploader_id", "view_count", "like_count",
                "tags", "categories", "chapters", "subtitles", "comments"} & set(res)),
          "keys=" + ",".join(sorted(res)))
    src = res["sources"][0]
    check("只投影必要字段", set(src) <= {
        "format_id", "label", "height", "fps", "ext", "protocol", "is_hls",
        "bitrate_kbps", "size_bytes", "size_estimate_bytes",
        "direct_url", "play_url", "requires_headers"},
        "srckeys=" + ",".join(sorted(src)))
    check("direct_url 即原始直链", src["direct_url"] == TARGET, src["direct_url"])

    print("== 播放代理 ==")
    play = BASE + src["play_url"]
    r = c.get(play)
    check("代理取流 200", r.status_code == 200, f"ct={r.headers.get('content-type')} len={len(r.content)}")
    check("代理透传内容", r.headers.get("content-type", "").startswith("video/"), str(r.headers.get("content-type")))
    check("不泄漏站点请求头", "set-cookie" not in " ".join(r.headers.keys()).lower())

    r = c.get(play, headers={"Range": "bytes=100-199"})
    check("Range 断点续传 206", r.status_code == 206 and len(r.content) == 100,
          f"{r.status_code} {len(r.content)}B crange={r.headers.get('content-range')}")

    r = c.head(play)
    check("HEAD 探活 200 且带长度", r.status_code == 200 and r.headers.get("content-length"),
          f"{r.status_code} len={r.headers.get('content-length')} ct={r.headers.get('content-type')}")
    check("HEAD 不返回 body", len(r.content) == 0, f"{len(r.content)}B")

    r = c.get(BASE + "/api/health", )
    r = c.head(BASE + "/api/health")
    check("health HEAD 200", r.status_code == 200, str(r.status_code))
    r = c.head(BASE + "/")
    check("首页 HEAD 200", r.status_code == 200, str(r.status_code))

    r = c.get(play + "&dl=1&filename=bbb_1080P.mp4")
    disp = r.headers.get("content-disposition", "")
    check("下载模式 attachment", "attachment" in disp and 'filename="bbb_1080P.mp4"' in disp, disp[:110])

    print("== 令牌防护 ==")
    forged = BASE + "/api/stream?u=" + base64.urlsafe_b64encode(b"https://evil.example/x.mp4").decode().rstrip("=") + "&t=1.aaaa"
    r = c.get(forged)
    check("伪造签名被拒 403", r.status_code == 403, r.text[:70])
    r = c.get(BASE + "/api/stream?u=not-base64!!!&t=x")
    check("非法编码被拒", r.status_code in (400, 403), f"{r.status_code} {r.text[:60]}")

    print("== 令牌防护（remux 复用同一入口校验）==")
    forged_u = base64.urlsafe_b64encode(b"https://evil.example/x.m3u8").decode().rstrip("=")
    r = c.post(f"{BASE}/api/remux?u={forged_u}&t=1.aaaa", content=b"")
    check("伪造令牌发起合并被拒 403", r.status_code == 403, r.text[:70])
    r = c.get(f"{BASE}/api/remux/nosuchjob/file")
    check("未知合并任务取文件 404", r.status_code == 404, str(r.status_code))

    print("== HLS 直链：标题兜底 + 服务端合并下载 ==")
    HLS_TARGET = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
    r = c.post(f"{BASE}/api/extract", json={"url": HLS_TARGET})
    check("提取 m3u8 直链 202", r.status_code == 202, r.text[:80])
    hls_job = r.json()["job_id"]
    hstate = None
    for _ in range(40):
        time.sleep(1.5)
        hstate = c.get(f"{BASE}/api/jobs/{hls_job}").json()
        if hstate["status"] in ("done", "failed"):
            break
    check("m3u8 提取完成", hstate and hstate["status"] == "done",
          json.dumps(hstate and hstate.get("error"), ensure_ascii=False)[:120])
    if hstate and hstate["status"] == "done":
        hres = hstate["result"]
        check("裸 m3u8 不再显示 URL 哈希当标题",
              hres["title"] == "未命名视频（m3u8 直链不含标题信息）", hres["title"][:50])
        hsrc = hres["sources"][hres["default_index"]]
        qs = dict(p.split("=", 1) for p in hsrc["play_url"].split("?", 1)[1].split("&"))
        ffmpeg_ok = c.get(f"{BASE}/api/health").json().get("ffmpeg_available")
        if not ffmpeg_ok:
            # 降级路径：下载拿到的应是清单原文（VLC 可打开），而不是坏链接
            r = c.get(BASE + hsrc["play_url"] + "&dl=1")
            check("无 ffmpeg 时降级下载清单", r.status_code == 200 and "#EXTM3U" in r.text[:20],
                  f"ct={r.headers.get('content-type')}")
            print("  SKIP  服务端合并（本机未装 ffmpeg，降级路径已验证）")
        else:
            from urllib.parse import quote
            r = c.post(f"{BASE}/api/remux?u={qs['u']}&t={qs['t']}"
                       f"&filename={quote('合并测试.mp4')}", content=b"")
            check("POST /api/remux 202", r.status_code == 202, r.text[:80])
            rj = r.json()["job_id"]
            rst = None
            for _ in range(120):
                time.sleep(2)
                rst = c.get(f"{BASE}/api/jobs/{rj}").json()
                if rst["status"] in ("done", "failed"):
                    break
            check("合并任务完成", rst and rst["status"] == "done",
                  (json.dumps(rst and rst.get("error"), ensure_ascii=False) or "")[:140])
            if rst and rst["status"] == "done":
                rr = rst["result"]
                check("结果不含服务器文件路径", "path" not in json.dumps(rr),
                      "kind=%s size=%s" % (rr.get("kind"), rr.get("size_bytes")))
                dl = c.get(f"{BASE}/api/remux/{rj}/file")
                check("取回的是真 MP4", dl.status_code == 200 and dl.content[4:8] == b"ftyp",
                      f"{len(dl.content)}B head={dl.content[:8]!r}")
                check("合并产物可重复下载（中断后能再点）",
                      c.get(f"{BASE}/api/remux/{rj}/file").status_code == 200)

    print("== HLS 清单改写（本地构造） ==")
    # 用测试用的清单服务器不易构造，这里直接验证改写函数的行为
    sys.path.insert(0, "backend")
    from app.stream_proxy import rewrite_manifest  # noqa: E402

    m3u8 = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
            "low/prog_index.m3u8\n#EXT-X-KEY:METHOD=AES-128,URI=\"https://cdn.k/key.bin\"\n"
            "#EXT-X-MAP:URI=\"init.mp4\"\nseg-1.ts\n")
    out = rewrite_manifest(m3u8, "https://cdn.example.com/v/ master.m3u8")
    check("子清单走代理", "/api/stream?u=" in out and "low/prog_index.m3u8" not in out.replace("\n", "\n"), out.count("/api/stream"))
    check("EXT-X-KEY 走代理", 'URI="/api/stream' in out or 'URI=/api/stream' in out)
    check("EXT-X-MAP 走代理", "#EXT-X-MAP:URI=\"/api/stream" in out)
    check("分片走代理", out.count("/api/stream") >= 4, str(out.count("/api/stream")))

    print("== 页面兜底解析（yt-dlp 无提取器的站点） ==")
    # 全部离线构造，不依赖任何外网站点是否还在/改版
    from app import scraper  # noqa: E402

    BS = chr(92)                     # 反斜杠，避开源码里的转义歧义
    esc = BS * 2                     # JSON 双重转义：\\/\\/
    # 真实页面里 `https://` 写作 `https:\\/\\/`，即「每个斜杠前各带两个反斜杠」，
    # 所以拼接时必须是 esc+"/" 成对出现两次，写成一次会得到 https:/hls…（坏 URL）。
    slash2 = esc + "/" + esc + "/"
    real_url = "https://hls.example.cn/v/abc.m3u8?auth_key=1-2-3"
    page_html = (
        "<html><head>"
        "<meta property='og:title' content='测试视频标题 &amp; 副标题'>"
        "<title>测试视频标题 &amp; 副标题 - 某站点</title>"
        "</head><body><h1>不该被选中的 H1</h1>"
        '<script>var p = {"video":{"url":"https:' + slash2
        + "hls.example.cn" + esc + "/v" + esc + "/abc.m3u8?auth_key=1-2-3"
        + '","type":"hls"}};</script>'
        '<script>var ad={"ad_slot":"video_player_ads","url":"https://ads.example.net/pre.m3u8"};</script>'
        "</body></html>"
    )

    check("优先取 og:title 并解 HTML 实体",
          scraper.page_title(page_html) == "测试视频标题 & 副标题",
          scraper.page_title(page_html)[:40])

    media = scraper.page_media(page_html, "https://site.example/post/1")
    check("能吃下 JSON 双重转义的直链",
          real_url in media, str(media[:2]))
    check("广告位直链被排到正片之后",
          bool(media) and "ads.example.net" not in media[0], media[0][:60] if media else "none")
    check("无效/相对地址不会混进结果",
          all(m.startswith("http") for m in media), str(media[:1]))

    # 取不到内容时必须是"空结果"而不是抛异常（兜底路径不许把主流程带崩）
    check("空白页面返回空结果而非异常",
          scraper.page_title("") == "" and scraper.page_media("", "https://a.b/") == [])

    # 一页多视频：每个视频常带多个编码变体（video / video_h265），
    # 必须「同块内的变体合并、不同块各留一条」，否则多视频页只出一个（实际反馈过）。
    multi = (
        '<script>var p1={"video":{"url":"https://cdn1.example/a.m3u8?auth_key=1"},'
        '"video_h265":{"url":"https://cdn1.example/a_h265.m3u8?auth_key=1"}};</script>'
        '<script>var p2={"video":{"url":"https://cdn2.example/b.m3u8?auth_key=2"},'
        '"video_h265":{"url":"https://cdn2.example/b_h265.m3u8?auth_key=2"}};</script>'
    )
    mm = scraper.page_media(multi, "https://site.example/post/2")
    check("一页两个视频 → 出两条源（不再折叠成一个）", len(mm) == 2, str(len(mm)))
    check("同一视频的 h265 变体被合并掉（不占用视频名额）",
          not any("_h265" in u for u in mm), str([u.rsplit("/", 1)[-1] for u in mm]))
    check("两个视频各自取到自己的 h264 主源",
          any("/a.m3u8" in u for u in mm) and any("/b.m3u8" in u for u in mm),
          str([u.rsplit("/", 1)[-1] for u in mm]))

    # 标题来源：WordPress 类主题常常没有 og:title，只有 Schema.org 微数据 + h1.post-title
    wp = ('<html><head><meta itemprop="headline" content="微数据里的标题">'
          '<title>微数据里的标题 - 某站点</title></head>'
          '<body><h1 class="post-title" itemprop="name headline">h1 里的标题</h1></body></html>')
    check("读得到 itemprop=headline（无 og:title 时）",
          scraper.page_title(wp) == "微数据里的标题", scraper.page_title(wp))

    wp2 = ('<html><head><title>只有 title 的页面 - 某站点</title></head>'
           '<body><h2 class="post-title">正文标题节点</h2></body></html>')
    check("无微数据时退回主题的 .post-title 节点",
          scraper.page_title(wp2) == "正文标题节点", scraper.page_title(wp2))

    # <title> 的站点后缀清洗：只裁「像站点名」的尾巴，内容词一律保留
    check("清洗 <title> 的站点后缀",
          scraper.page_title("<title>我的视频 - 某某站</title>") == "我的视频",
          scraper.page_title("<title>我的视频 - 某某站</title>"))
    check("不误伤正常的「标题 - 第二季」",
          scraper.page_title("<title>某部纪录片 - 第二季</title>") == "某部纪录片 - 第二季",
          scraper.page_title("<title>某部纪录片 - 第二季</title>"))
    check("清洗域名式站点尾巴",
          scraper.page_title("<title>精彩片段 | www.demo.com</title>") == "精彩片段",
          scraper.page_title("<title>精彩片段 | www.demo.com</title>"))

    print("== 批量下载 ==")
    # 先用纯逻辑单测把「从混排文本里挑 URL」这一环钉死（不依赖网络）
    from app import batch as batch_mod  # noqa: E402

    picked = batch_mod.parse_urls(
        "看这个 https://a.example/x.mp4 还有\nhttps://b.example/y.m3u8\n"
        "重复的 https://a.example/x.mp4 结尾。")
    check("从混排文本里挑出并去重链接",
          picked == ["https://a.example/x.mp4", "https://b.example/y.m3u8"], str(picked))
    check("中文标点紧邻链接时不误吞",
          batch_mod.parse_urls("链接：https://c.example/z.mp4，好") == ["https://c.example/z.mp4"],
          str(batch_mod.parse_urls("链接：https://c.example/z.mp4，好")))
    check("非 http 文本不产出链接", batch_mod.parse_urls("没有链接的一段话") == [])

    # 坏链接不该整批拒绝：一条内网 + 一条有效 → 202，坏的那条标 failed
    r = c.post(f"{BASE}/api/batch",
               json={"text": "http://127.0.0.1/secret.mp4\n" + TARGET, "label": "冒烟批量"})
    check("含坏链接的批次仍被接受 202", r.status_code == 202, r.text[:90])
    if r.status_code == 202:
        b = r.json()
        check("坏链接被单独标记而非拒绝整批",
              b.get("rejected") == 1 and b.get("total") == 2, json.dumps(b, ensure_ascii=False))
        bid = b["batch_id"]

        bstate = None
        for _ in range(120):
            time.sleep(2)
            bstate = c.get(f"{BASE}/api/batch/{bid}").json()
            if bstate["finished"]:
                break
        check("批次进入 finished（含打包阶段结束）",
              bstate and bstate["finished"], json.dumps(bstate and bstate.get("counts")))
        check("好链接成功、坏链接失败",
              bstate and bstate["counts"].get("done") == 1 and bstate["counts"].get("failed") == 1,
              json.dumps(bstate and bstate["counts"]))

        if bstate and bstate.get("pack_state") == "packed":
            dl = c.get(f"{BASE}/api/batch/{bid}/file")
            check("取回的是合法 zip",
                  dl.status_code == 200 and dl.content[:2] == b"PK",
                  f"{dl.status_code} {len(dl.content)}B")
            if dl.status_code == 200:
                import io as _io
                import zipfile as _zip
                with _zip.ZipFile(_io.BytesIO(dl.content)) as zf:
                    names = zf.namelist()
                    check("zip 内只放成功的条目", len(names) == 1, str(names))
                    check("zip 可正常解压校验", zf.testzip() is None)
        else:
            print("  SKIP  打包产物校验（pack_state=%s）" % (bstate or {}).get("pack_state"))

        check("批次可删除", c.delete(f"{BASE}/api/batch/{bid}").status_code == 200)
    check("未知批次 404", c.get(f"{BASE}/api/batch/nosuchbatch").status_code == 404)

    print("== 定时任务 ==")
    r = c.post(f"{BASE}/api/schedules", json={
        "urls": [TARGET], "mode": "interval", "every_minutes": 600, "label": "冒烟计划"})
    check("建计划 201", r.status_code == 201, r.text[:90])
    if r.status_code == 201:
        s = r.json()
        sid = s["schedule_id"]
        check("已算出下次触发时间", bool(s.get("next_run_at")), str(s.get("next_run_at")))
        check("计划出现在列表里",
              any(x["schedule_id"] == sid for x in c.get(f"{BASE}/api/schedules").json()["schedules"]))

        r2 = c.patch(f"{BASE}/api/schedules/{sid}", json={"enabled": False})
        check("停用后不再有下次时间", r2.json().get("next_run_at") is None)
        r3 = c.patch(f"{BASE}/api/schedules/{sid}", json={"enabled": True, "mode": "daily", "at": "03:07"})
        check("重新启用会重算下次时间",
              bool(r3.json().get("next_run_at")) and r3.json().get("at") == "03:07",
              f"at={r3.json().get('at')} next={r3.json().get('next_run_at')}")

        check("计划可删除", c.delete(f"{BASE}/api/schedules/{sid}").status_code == 200)
    check("未知计划 404", c.get(f"{BASE}/api/schedules/nosuch").status_code in (404, 405))

    # 定时执行的"可见性"：一次执行可能几十分钟（HLS 逐条合并），若状态要等结束才更新，
    # 用户会以为定时没生效（实际反馈过）。这里断言：手动触发后**立刻**就是 running。
    r = c.post(f"{BASE}/api/schedules", json={
        "urls": [TARGET], "mode": "interval", "every_minutes": 600, "label": "可见性"})
    if r.status_code == 201:
        vsid = r.json()["schedule_id"]
        c.post(f"{BASE}/api/schedules/{vsid}/run")
        seen = None
        for _ in range(20):
            time.sleep(0.5)
            seen = next((x for x in c.get(f"{BASE}/api/schedules").json()["schedules"]
                         if x["schedule_id"] == vsid), None)
            if seen and seen["last_status"] == "running":
                break
        check("触发后立刻可见 running（不等执行结束）",
              bool(seen) and seen["last_status"] == "running",
              (seen or {}).get("last_message", "no schedule"))
        check("running 时 last_run_at 已写入（界面能显示开始时间）",
              bool(seen) and seen.get("last_run_at"), str((seen or {}).get("last_run_at")))
        check("running 时已关联批次 id（界面能指向进度）",
              bool(seen) and bool(seen.get("last_batch")), str((seen or {}).get("last_batch")))

        # 等它跑完，确认终态与 run_count 正确
        final = None
        for _ in range(120):
            time.sleep(2)
            final = next((x for x in c.get(f"{BASE}/api/schedules").json()["schedules"]
                          if x["schedule_id"] == vsid), None)
            if final and final["last_status"] in ("done", "failed"):
                break
        check("执行最终落到终态并记录结果",
              bool(final) and final["last_status"] == "done" and final["run_count"] == 1,
              (final or {}).get("last_message", "no schedule"))
        check("终态后重算了下次触发时间",
              bool(final) and bool(final.get("next_run_at")), str((final or {}).get("next_run_at")))
        c.delete(f"{BASE}/api/schedules/{vsid}")

    print("== 状态持久化文件 ==")
    from app.config import config as _cfg  # noqa: E402
    check("state.json 落在预期位置",
          os.path.isfile(_cfg.STORE_FILE), _cfg.STORE_FILE)
    if os.path.isfile(_cfg.STORE_FILE):
        raw = json.loads(open(_cfg.STORE_FILE, encoding="utf-8").read())
        check("持久化结构带 schema 版本号", raw.get("schema") == 1, str(raw.get("schema")))

    print("== 体积预估与合并进度 ==")
    # 清单分析：纯逻辑单测（不依赖网络），把估算公式钉死
    from app import playlist  # noqa: E402

    check("人读体积文案", playlist.describe_size(20601012) == "19.6 MB",
          playlist.describe_size(20601012))
    check("零/空值不产出文案",
          playlist.describe_size(0) == "" and playlist.describe_size(None) == "")

    # 拿真实清单验证「预估 vs 实际」：这条的产物实测 19.1 MB
    est = playlist.analyze("https://test-streams.mux.dev/x36xhzz/url_2/"
                           "193039199_mp4_h264_aac_ld_7.m3u8")
    if est.get("estimate_bytes"):
        mb = est["estimate_bytes"] / 1048576
        check("清单分析给出时长与预估体积",
              bool(est.get("duration")) and 15 <= mb <= 25,
              f"时长={est.get('duration')} 预估={mb:.1f} MB（实际 19.1 MB）")
    else:
        print("  SKIP  清单分析（网络不可达）")

    # 提取结果里应带 size_estimate_bytes（HLS 源下载前就能看到大小）
    r = c.post(f"{BASE}/api/extract", json={
        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"})
    if r.status_code == 202:
        hjob = r.json()["job_id"]
        hst = None
        for _ in range(60):
            time.sleep(1.5)
            hjob_st = c.get(f"{BASE}/api/jobs/{hjob}").json()
            if hjob_st["status"] in ("done", "failed"):
                hst = hjob_st
                break
        if hst and hst["status"] == "done":
            hsrc = hst["result"]["sources"][hst["result"]["default_index"]]
            check("HLS 源带体积预估（下载前可见）",
                  bool(hsrc.get("size_estimate_bytes")), str(hsrc.get("size_estimate_bytes")))
            # 预估必须与"真正会下载的那一档"一致，不能是主清单最高档（差过 25 倍）
            if hsrc.get("bitrate_kbps"):
                implied = hsrc["size_estimate_bytes"] * 8 / max(hst["result"]["duration"] or 1, 1) / 1000
                check("预估码率与该源自身码率吻合（不是主清单最高档）",
                      abs(implied - hsrc["bitrate_kbps"]) < hsrc["bitrate_kbps"] * 0.35,
                      f"预估隐含 {implied:.0f} kbps vs 源声明 {hsrc['bitrate_kbps']} kbps")

            # 合并过程应持续上报百分比，且不超过 99%（到 DONE 才 100%）
            qs = dict(p.split("=", 1) for p in hsrc["play_url"].split("?", 1)[1].split("&"))
            r2 = c.post(f"{BASE}/api/remux?u={qs['u']}&t={qs['t']}&filename=进度.mp4",
                        content=b"")
            if r2.status_code == 202:
                prj = r2.json()["job_id"]
                samples: list[int] = []
                final = None
                for _ in range(400):
                    time.sleep(1.5)
                    final = c.get(f"{BASE}/api/jobs/{prj}").json()
                    if final["status"] in ("done", "failed"):
                        break
                    if final.get("progress") is not None:
                        samples.append(final["progress"])
                check("合并过程中上报了百分比进度", len(samples) >= 3, f"{len(samples)} 个采样")
                check("进度单调不减且在 1..99 之间",
                      samples == sorted(samples) and all(0 <= v <= 99 for v in samples),
                      str(samples[:12]))
                check("同时上报已写字节与预估字节",
                      bool(final and (final.get("written_bytes") or final.get("estimate_bytes"))),
                      f"written={final and final.get('written_bytes')} "
                      f"est={final and final.get('estimate_bytes')}")
                check("完成时进度归 100",
                      bool(final) and final["status"] == "done" and final.get("progress") == 100,
                      str(final and final.get("progress")))
        else:
            print("  SKIP  HLS 预估与进度（提取未完成）")

    print(f"\n结果：{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
