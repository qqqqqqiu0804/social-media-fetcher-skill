# Social Media Fetcher (社媒内容抓取)

## 技能描述

从**微博**和 **Threads** 抓取帖子的原始媒体：视频最高画质 + 全部原图 + 元数据（正文、作者、时间、互动数）。
全部单文件、零第三方依赖（图片对照图需 Pillow，抽帧需 imageio-ffmpeg，都是可选）。

## 适用场景

- 用户发来一条微博 / Threads 链接，问「能不能下」「怎么下高画质」
- 需要把一条帖子里的图和视频整体存下来做素材
- 需要确认「下到的到底是不是那一档画质」（用 `probe_mp4.py` 自证，而不是信下载工具的嘴）
- 通用工具（yt-dlp / you-get）卡在微博 720p、或者根本没有 Threads extractor

## 不适用

- 需要绕过访问控制的内容（私密号、已删帖、付费墙）—— 本技能只处理公开可见的帖子
- 批量爬取整个账号的时间线（这是单帖抓取器，批量要自己在外面套循环并控制频率）
- 抖音 / 小红书 / B站 —— 各走各的链路，不在本技能范围

## 三个脚本

| 脚本 | 干什么 | 依赖 |
|---|---|---|
| `scripts/weibo_video.py` | 微博视频**最高画质**（1080p/720p/480p 全档可选） | 纯标准库 |
| `scripts/threads_dl.py` | Threads 帖子视频 + 图片 | 纯标准库 |
| `scripts/probe_mp4.py` | 不解码、不装 ffprobe，直接读 mp4 容器自证分辨率/时长/编码 | 纯标准库 |
| `scripts/contact_sheet.py` | 把一批图铺成一张带编号对照图，一次看全 | Pillow（可选） |

## 快速开始

### 微博视频

```bash
# 1) 先看有哪些档，不下载
python scripts/weibo_video.py "https://weibo.com/7735668873/QgQGBDZ9t" \
    --session-json ~/weibo-exp/data/session.json --list

# 2) 下最高档
python scripts/weibo_video.py "https://weibo.com/7735668873/QgQGBDZ9t" \
    --session-json ~/weibo-exp/data/session.json \
    --q 1080p -o ./out

# 3) 自证不是伪高清
python scripts/probe_mp4.py ./out/*.mp4
```

cookie 三种给法（任一）：
- `--session-json <path>`：weibo-exp 风格的 `{"cookies":[{name,value,domain,expires}]}`，**本机首选**
- `--cookie "SUB=...; SUBP=..."`
- `--cookie-file cookies.txt`（Netscape 格式）
- 环境变量 `WEIBO_COOKIE`

### Threads

```bash
# 有代理、想拿 Meta 原始直链
python scripts/threads_dl.py "<threads url>" --provider page --proxy http://127.0.0.1:7897 -o ./out

# 本机连不上 Meta 时（默认 auto 会自己降级）
python scripts/threads_dl.py "<threads url>" --provider mirror --images -o ./out

# 只解析不下载
python scripts/threads_dl.py "<threads url>" --json
```

### 对照图

```bash
python scripts/contact_sheet.py ./out --cols 4
# → ./out/contact.jpg
```

## 核心结论（为什么这么做）

### 1. 微博高画质不在 `stream_url` 里

`page_info.media_info` 有一堆 `mp4_hd_url` / `stream_url_hd` 看着像高清，实测**全是同一条 720p**。
真正的分档在 `playback_list[]`：

```
playback_list[0].meta.quality_label = "1080p"
playback_list[0].play_info.url      = https://f.video.weibocdn.com/o0/xxx.mp4?label=mp4_1080p&...
playback_list[0].play_info.bitrate  = 1494746
playback_list[0].play_info.size     = 84231954
```

所以通用工具只读 `stream_url` 就永远只能拿到 720p。**这是本技能存在的核心理由。**

### 2. 微博视频链路三个坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 直链带 `ssig` + `Expires` | 约 1 小时过期 | 过期重跑脚本重新取，不用换链接 |
| 对直链发 `HEAD` | 403 Forbidden | 只能流式 `GET` |
| 480p 档 | url 与 720p 完全相同 | 脚本会标 `(复用 720p 码流)`，别以为抓错了 |

微博视频号目前**最高就是 1080p**，`playback_list` 里不会有 4K。`h265_mp4_hd` / `inch_5_mp4_hd` 这些字段常年是空串，不代表有更高档。

### 3. 微博图文帖和老帖会撞登录墙

`ajax/statuses/show` 对**部分帖子**（尤其是老帖、非视频帖）返回
`{"ok":-100,"url":"https://weibo.com/login.php?..."}`，换 Referer、加 UA 都绕不过。
这不是 cookie 坏了 —— 同一条 cookie 抓视频帖正常。遇到就换 `m.weibo.cn` 链路或直接用浏览器（puppeteer 拦 XHR）。

### 4. Threads 没有 yt-dlp extractor

`yt-dlp 2026.10.26` 对 `threads.com/@x/post/CODE` 只匹配 `generic`，必然 `Unsupported URL`。
官方 issue 被判 duplicate（[yt-dlp#15086](https://github.com/yt-dlp/yt-dlp/issues/15086)），
`supportedsites.md` 里只有 `Instagram`，没有 Threads。

两条路：
- `--provider page`：自己抓 `/embed` 或正帖页。页面里帖子 JSON 转义内联（`\/`、`\"`），
  流程是「反转义 → 正则定位 `"video_versions": [` → 括号配平切 JSON → `json.loads`」。
  正帖页必须带 `Sec-Fetch-Dest: document` 等导航头，否则只回 ~256KB 空壳。
- `--provider mirror`：走第三方代解析服务（见 `references/threads.md`）。

### 5. 媒体 URL 一定要过 `html.unescape`

页面里是 `&amp;`，直接拿去请求会 403。这是最常见的「明明有链接却下不动」。

## 环境要求

- Python 3.9+（用了 `X | None` 语法，3.10+ 更稳）
- 无第三方依赖；`contact_sheet.py` 需要 Pillow，`probe_mp4.py --frame` 需要 imageio-ffmpeg

## 项目结构

```
social-media-fetcher-skill/
├── SKILL.md
├── README.md
├── LICENSE
├── scripts/
│   ├── weibo_video.py      # 微博视频全档位
│   ├── threads_dl.py       # Threads 视频 + 图片
│   ├── probe_mp4.py        # mp4 容器解析（自证画质）
│   └── contact_sheet.py    # 对照图
└── references/
    ├── weibo.md            # 微博接口实测记录与坑
    └── threads.md          # Threads 抓取路线与镜像服务细节
```

## 自测

三个脚本都内置离线自测，不联网即可验证解析逻辑：

```bash
python scripts/weibo_video.py --selftest
python scripts/threads_dl.py --selftest
python scripts/probe_mp4.py <任意 mp4>
```
