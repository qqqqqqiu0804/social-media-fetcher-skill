# Threads 媒体抓取 · 实测记录

> 本机实测（2026-11-06，Windows）。Threads 是 Meta 系，网络与页面结构都变得快。

## 零、先确认网络

动手前先搞清楚出口能不能到 Meta。本机实测（2026-11-06）：

| 目标 | 结果 |
|---|---|
| `www.threads.com` | TCP 443 **超时**，DNS 返回 `104.244.46.85`（那是 Twitter 网段，污染） |
| `www.threads.net` | 超时，DNS `199.96.63.163` |
| `www.instagram.com` | 超时，DNS `65.49.26.97` |
| `scontent.cdninstagram.com` | 超时，DNS `69.30.25.21` |
| `github.com` / `weibo.com` / `cdn.jsdelivr.net` | 正常 |

DoH 查到的真实地址（`doh.pub`）：

```
www.threads.com      → 157.240.24.63
www.instagram.com    → 157.240.24.174
api.instagram.com    → 157.240.24.63   ← 这个 TCP + TLS 都通
scontent.cdninstagram.com → 157.240.24.63
```

**注意**：TCP 能连上不等于能用。实测 `157.240.24.63:443` TCP 握手成功，
但 TLS 握手 `TimeoutError` / `ConnectionResetError` —— 典型的 SNI 阻断。
所以「改 hosts 到真实 IP」这条路在本机走不通。

本机装了 Clash Verge（`C:\Program Files\Clash Verge\`），但：

```
verge.yaml:  enable_system_proxy: false, enable_tun_mode: null
clash-verge.yaml:  mixed-port: 7897
netstat:     7897 无监听
logs/latest.log: "Application exited" @ 09:50
```

即**客户端在但没运行**。要用代理得先把 Verge 起起来，然后
`--proxy http://127.0.0.1:7897`（不是 7890，这台配的是 7897）。

## 一、为什么 yt-dlp 不行

`yt-dlp 2026.10.26` 对 `https://www.threads.com/@x/post/CODE` 只匹配到 `generic` extractor，
必然 `Unsupported URL`。官方 issue
[yt-dlp#15086](https://github.com/yt-dlp/yt-dlp/issues/15086) 被判定为 duplicate，
`supportedsites.md` 里只有 `Instagram`，**没有 Threads**。

## 二、路线 A：`--provider page`（自己抓）

### A1. `/embed` 页（首选）

```
https://www.threads.com/@<user>/post/<CODE>/embed
```

匿名请求通常就能拿到内联 JSON。注意：

- 页面里帖子 JSON 是**转义后内联**的：`\/`、`\"`
- 处理流程：反转义 → 正则定位 `"video_versions": [` / `"image_versions2": {`
  → **按括号配平切出完整 JSON 字面量** → `json.loads`
- 按括号配平切比整页 `json.loads` 稳：Threads 改外层结构不会立刻挂

### A2. 正帖页

必须带浏览器式导航头，否则只回 ~256 KB 空壳：

```
Sec-Fetch-Dest: document
Sec-Fetch-Mode: navigate
Sec-Fetch-Site: none
Sec-Fetch-User: ?1
Upgrade-Insecure-Requests: 1
```

匿名大概率仍拿空壳，需要 `sessionid` cookie。

### A3. `og:video` / `og:image` meta 兜底

最后一道，能拿到通常是最低档。

### URL 还原

媒体 URL 必须过 `html.unescape`（`&amp;` → `&`），否则 CDN 带签名参数会 403。
这是最常见的「明明有链接却下不动」。

## 三、路线 B：`--provider mirror`（镜像代解析）

本机直连 Meta 全断时唯一可行。用 `threadsphotodownloader.com`（Rails + Turbo）：

### 完整流程

```
1. GET  https://threadsphotodownloader.com/
   → Set-Cookie: _jumpstart_app_session=...
   → 页面里 <input name="authenticity_token" value="...">

2. POST https://threadsphotodownloader.com/download
   Content-Type: application/x-www-form-urlencoded
   body: authenticity_token=<token>&url=<帖子URL>
   → 302 到 /download/<job_id>

3. 结果页里内嵌：
   <turbo-frame id="media_resolution" src="/download/<job_id>/status">
   
4. GET /download/<job_id>/status
   Turbo-Frame: media_resolution
   Accept: text/vnd.turbo-stream.html, text/html, */*
   Referer: https://threadsphotodownloader.com/download/<job_id>
   → 首次就可能有结果，不用真等

5. 结果片里的链接形如：
   <a download href="https://cdn.threadsphotodownloader.com/<origin-host>/<path>?<query>">
```

### 关键细节

**cookie 和 token 缺一不可。** 只带 `authenticity_token` 不带会话 cookie，
status 请求一律 `404`。必须先 `GET /` 建立会话，同一个 `CookieJar` 走完四步。

**镜像 CDN 的链接怎么拆。** 形如：

```
https://cdn.threadsphotodownloader.com/scontent.cdninstagram.com/v/t51.82787-15/xxx_n.jpg?stp=...
                                 └───────── 前缀 ─────────┘└──── Meta 原始 host + path ────┘
```

去掉第一段就是 Meta 原始地址。脚本会把两者都打出来（`media.originals` 是原始地址）。

**它不是通用反代。** 手搓 `cdn.threadsphotodownloader.com/<任意host>/<path>` 会 404 或
`403 Bad URL hash`。必须先走完 1–4 拿到带签名的整条链接。

**下载必须带 Referer。** 直连镜像 CDN 不带 `Referer: https://threadsphotodownloader.com/`
会回 `403 Bad URL hash`。

**图片不是绝对原图。** 镜像给的是 `stp=dst-jpg_e35_tt6` 档（最长边约 2139），
要绝对原图只能在能连 Meta 的环境重取。

### 实测输出（`@nigonigoaoao/post/DfTnNxHmt7-`）

```
视频 1 条 / 图片 9 张
视频：732x718, 6.57s, h264 High, 541 kb/s, 447 KB
图片：最长边 860 / 1280 / 1430 / 960 / 2139 / 2160 / 1712 / 1512 / 2160
```

视频 732×718 就是帖子源里的最高档，镜像不提供码率分档。

## 四、`cd.threadsphotodownloader.com` 之外试过的服务

| 服务 | 结果 |
|---|---|
| `api-tp.threadsdownloader.com/threads/post/media` | 200 但返回占位图（`threadsdownloader-nolongerworks.png`），已废弃 |
| `api-tp.threadsdownloader.com/insta/post/media` | 400 `No videos found` |
| `api-x.threadsdownloader.com` | 404 `Cannot GET` |
| `sssthreads.com` | 前端调的就是上面那个已废弃的 API |
| `kkinstagram.com` | 404（只处理 Instagram，不处理 Threads） |
| `ddinstagram.com` / `instafix.app` | DNS 不解析 |
| `graph.threads.net/v1.0/oembed` | 超时（Meta 域） |
| `r.jina.ai` | 首页可达，但代抓 threads 时远程断开 / 超时 |
| `api.allorigins.win` / `corsproxy.io` / `cors.isomorphic-git.org` 等公共 CORS 代理 | 全部失败（超时 / 403 / 无法解析） |
| `threadsdownloader.io` | 401 |
| `savethreads.net` | 域名已过期，落到 NameSilo 停放页 |

**目前只有 `threadsphotodownloader.com` 可用。**

## 五、GitHub 上现成的工具（不想自己抓）

| 项目 | 说明 |
|---|---|
| [AAndyProgram/SCrawler](https://github.com/AAndyProgram/SCrawler) | 2.2k★，.NET 桌面程序，明确支持 Threads，Windows 便携版，最成熟 |
| [corvardt/nostos](https://github.com/corvardt/nostos) | 本地 Web UI，Threads 走自研 scraper + 浏览器 cookie，代码干净可读（本技能 page 路线参考它，MIT） |
| [milancodess/universalDownloader](https://github.com/milancodess/universalDownloader) | Node/Express API，多平台统一接口 |
| [arsya371/prenivdlapp-cli](https://github.com/arsya371/prenivdlapp-cli) | CLI，Win/Linux/macOS/Termux |
| [jeiel85/threads-archiver](https://github.com/jeiel85/threads-archiver) | Tauri 桌面归档 App（Playwright 驱动） |
| [Vedant1521/fetchit](https://github.com/Vedant1521/fetchit) | TypeScript 终端下载器 |

## 六、通用注意

- 媒体链接带签名与过期（`oe=` / `_nc_oc`），解析完尽快下，别缓存 URL
- 只有图片的帖子不会产出 mp4，加 `--images`
- Threads 对匿名会话收紧得比较频繁，`/embed` 哪天不出 JSON 了就补 `sessionid` cookie
