# 微博媒体抓取 · 实测记录

> 全部结论均为本机实测（2026-11-06，Windows，Python 3.12）。
> 接口会变，下面的字段路径和坑是「当时确实如此」，不是永恒真理。

## 一、接口

```
GET https://weibo.com/ajax/statuses/show?id=<mid>&locale=zh-CN
```

必需请求头：

```
Cookie: SUB=...; SUBP=...; SSOLoginState=...
User-Agent: <桌面 Chrome UA>
Referer: https://weibo.com/<uid>/<mid>
X-Requested-With: XMLHttpRequest
```

`<mid>` 就是 URL 里那串 base62（`QgQGBDZ9t`），不是数字 id。
URL 形态兼容：`weibo.com/<uid>/<mid>`、`m.weibo.cn/detail/<数字id>`、`weibo.com/detail/<mid>`。

## 二、视频

### 字段路径

```
page_info.type == 11              ← 视频帖标识
page_info.object_type == "video"
page_info.media_info.playback_list[]     ← 真正的分档表
```

`playback_list` 每项：

```json
{
  "meta": { "label": "mp4_1080p", "quality_label": "1080p", "quality_desc": "高清",
            "quality_class": "HD", "quality_index": 1080 },
  "play_info": {
    "type": 1,                       // 1=视频流，3=进度条雪碧图（要排除）
    "mime": "video/mp4",
    "url": "https://f.video.weibocdn.com/o0/xxx.mp4?label=mp4_1080p&...&ssig=...",
    "width": 1920, "height": 1080,
    "bitrate": 1494746,
    "size": 84231954,
    "duration": 450.816,
    "video_codecs": "avc1.640032",
    "audio_codecs": "mp4a.40.5",
    "fps": 30
  }
}
```

### 别被骗的字段

| 字段 | 实际内容 |
|---|---|
| `media_info.stream_url` | 720p |
| `media_info.stream_url_hd` | **也是 720p**（名字带 hd，内容一样） |
| `media_info.mp4_hd_url` | 720p |
| `media_info.mp4_720p_mp4` | 720p |
| `media_info.h265_mp4_hd` / `inch_4/5/5.5_mp4_hd` / `hevc_mp4_720p` | 空串 |

实测某条 7分30秒视频：`stream_url_hd` 与 `mp4_hd_url` 完全同一个 URL，
而 1080p 只在 `playback_list[0]` 里出现。**这就是通用下载器只能拿 720p 的原因。**

### 480p 是假的

`playback_list` 里标 `480p`（`label: mp4_hd`，`quality_index: 480`）的那项，
`play_info.url` 与 720p **逐字节相同**，`width/height/bitrate/size` 也全一样。
不是抓错 —— 微博就是没为这条视频单独转 480p。

### 下载

```http
GET <play_info.url>
User-Agent: <桌面 Chrome UA>
Referer: https://weibo.com/
Accept: */*
```

- **不要发 `HEAD`**：返回 403 Forbidden。必须 `GET`。
- 直链带 `ssig=` 与 `Expires=`，有效期约 1 小时。过期后重打 `status/show` 拿新链接。
- 实测 80.3 MiB / 450 秒，20 秒下完（约 4 MB/s），不需要 cookie。

### 全档实测（`QgQGBDZ9t`）

| 档 | 分辨率 | 码率 | 体积 |
|---|---|---|---|
| 1080p | 1920×1080 | 1495 kb/s | 80.3 MiB |
| 720p | 1280×720 | 761 kb/s | 40.9 MiB |
| 480p | 1280×720 | 761 kb/s | 40.9 MiB（= 720p） |

`media_info.origin_total_bitrate` 是 6748692（6.75 Mbps），**但没有任何档位的直链对应这个码率**。
不要被这个数字误导以为有原画可拿。

## 三、图片

### 字段路径

```
pic_ids[]                    ← 顺序即发帖顺序
pic_infos[<pic_id>].original.{url,width,height}   ← 原图（首选）
pic_infos[<pic_id>].largest.{url,width,height}    ← 次选
pic_infos[<pic_id>].large.url
```

图片 URL 域名是 `wx1..wx4.sinaimg.cn`（不是 `f.video` 那套），匿名可访问，不需要签名。

尺寸档位（URL 里的路径段）：

| 段 | 含义 |
|---|---|
| `/orj1080/` | 1080 宽（够用，体积小） |
| `/large/` | 长边约 2000 |
| `/orj480/`、`/or180/`、`/or360/` | 各种小图 |

**取 `original` 拿原图**；只要缩略图的话把 `/large/` 换成 `/orj1080/` 即可。
`orj1080` 是「1080 宽等比」，竖图会按宽算，长边可能远超 1080。

## 四、登录墙（重要）

`ajax/statuses/show` 对部分帖子返回：

```json
{"ok": -100, "url": "https://weibo.com/login.php?url=https%3A%2F%2Fweibo.com%2F"}
```

实测触发条件（2026-11-06）：

| 帖子类型 | 结果 |
|---|---|
| 视频帖（`page_info.type == 11`） | **正常返回** |
| 老图文帖（2022 年的 mid） | **`ok:-100` 登录墙** |
| 2023 年图文帖 | `ok:-100` |

排除了这些可能：
- 换 `Referer`（`https://weibo.com/` / `https://weibo.com/<uid>/<mid>`）→ 都还是 -100
- 换 `m.weibo.cn` Referer → 直接 403
- cookie 本身没坏 —— 同一份 cookie 抓视频帖正常

**结论**：图文帖走 `ajax/statuses/show` 不可靠。要抓图文请走：
1. `m.weibo.cn/api/container/getIndex?type=uid&value=<uid>&containerid=...`（需要设备指纹，不稳）
2. **puppeteer 打开帖子页，拦截它自己发的 `/ajax/statuses/show` XHR** —— 这是最稳的，
   因为浏览器带着完整的访客/登录态。见「五、浏览器兜底」。

## 五、浏览器兜底（图文帖 / 老帖）

用真实浏览器打开帖子页，在 `page.on('response')` 里拦 `url.includes('/ajax/statuses/show')`，
把响应体存下来解析。要点：

- 用本机 Edge（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`）+ `puppeteer-core`
- UA 用 iPhone Safari，viewport 设 430×932 mobile
- `args: ['--no-sandbox', '--disable-blink-features=AutomationControlled']`
- 帖子页 1–2 秒内就发完那个 XHR，轮询等 10 秒足够
- **不要解析 DOM** —— 未登录时 DOM 是登录墙，但 XHR 响应是全的

## 六、cookie 来源

本机最方便的是 `weibo-exp` 项目的 session.json：

```json
{
  "version": 2,
  "cookies": [
    {"name": "SUB", "value": "...", "domain": ".weibo.com", "expires": 1790000000},
    ...
  ]
}
```

按 Chromium 规则过滤：
- `expires > 0 && expires < now` → 丢掉（`expires == 0` 表示会话 cookie，保留）
- domain 只留 `.weibo.com` / `.weibo.cn` / `.sina.com.cn`
- 同名 cookie 多个域时，更具体的域优先

实测有效的最小集合就是 `SUB` + `SUBP`（`SUB` 90 字符，`SUBP` 188–200 字符）。
`SSOLoginState` / `XSRF-TOKEN` 带上无害。

## 七、自证画质

不要信下载工具打印的「1080p」。用 `probe_mp4.py` 直接读容器：

```
文件      黄婷婷-...-1080p.mp4
体积      84,231,954 B (80.33 MiB)
时长      450.817 s
分辨率    1920x1080
视频编码  avc1
轨道      vide, soun
```

原理：解析 `moov/trak/tkhd` 拿宽高（定点数除以 65536），`moov/trak/mdia/minf/stbl/stsd` 拿 codec，
`moov/mvhd` 拿时长（`duration / timescale`）。

坑：`tkhd` 和 `stsd` 都**嵌在每个 `trak` 里**，不能只扫顶层 —— 有音视频两条轨时，
顶层扫会拿错或拿不到。必须先按 `hdlr` 认出 `vide` 轨，再进那条 trak 里找。
