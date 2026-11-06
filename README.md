# Social Media Fetcher

从**微博**和 **Threads** 抓取帖子的原始媒体 —— 视频最高画质 + 全部原图 + 元数据。

零第三方依赖（可选 Pillow / imageio-ffmpeg 只用于对照图和抽帧）。

## 为什么需要它

**通用下载器在微博上永远只能拿到 720p。** 微博的 `media_info.stream_url_hd`、
`mp4_hd_url` 这些看起来是高清的字段，实测**全部指向同一条 720p 码流**。
真正的 1080p 藏在 `playback_list[]` 里，每档一条独立直链 —— 只有带登录 cookie
打 `ajax/statuses/show` 才能拿到全表。

**Threads 连 extractor 都没有。** `yt-dlp 2026.10.26` 对 Threads 只匹配 `generic`，
必然 `Unsupported URL`（[yt-dlp#15086](https://github.com/yt-dlp/yt-dlp/issues/15086)）。

## 安装

```bash
git clone https://github.com/qqqqqqiu0804/social-media-fetcher-skill.git
cd social-media-fetcher-skill
# 无需 pip install
```

可选：

```bash
pip install Pillow          # contact_sheet.py
pip install imageio-ffmpeg  # probe_mp4.py --frame
```

## 用法

### 微博视频（全档位可选）

```bash
# 看有哪些档
python scripts/weibo_video.py "https://weibo.com/7735668873/QgQGBDZ9t" \
    --session-json ~/weibo-exp/data/session.json --list

# 下 1080p
python scripts/weibo_video.py "https://weibo.com/7735668873/QgQGBDZ9t" \
    --session-json ~/weibo-exp/data/session.json -o ./out
```

输出示例：

```
[*] mid: QgQGBDZ9t

可选档位：
  1080p    1920x1080    1495 kb/s  80.3 MiB
  720p     1280x720      761 kb/s  40.9 MiB
  480p     1280x720      761 kb/s  40.9 MiB  (复用 720p 码流)

下载 1080p (1920x1080)
  → ./out/QgQGBDZ9t-1080p.mp4
  完成 80.3 MiB (4.0 MiB/s)
```

### Threads

```bash
# 有代理（走自己抓，拿 Meta 原始直链）
python scripts/threads_dl.py "<threads url>" --provider page \
    --proxy http://127.0.0.1:7897 -o ./out

# 直连不通（默认 auto 自己降级，也可强制）
python scripts/threads_dl.py "<threads url>" --provider mirror --images -o ./out

# 只解析不下载
python scripts/threads_dl.py "<threads url>" --json
```

### 自证画质

```bash
python scripts/probe_mp4.py ./out/*.mp4
```

```
文件      QgQGBDZ9t-1080p.mp4
体积      84,231,954 B (80.33 MiB)
时长      450.817 s
分辨率    1920x1080
视频编码  avc1
轨道      vide, soun
```

不装 ffprobe，纯 `struct` 解析 mp4 容器，用来验证「下到的真的是这一档」。

### 对照图

```bash
python scripts/contact_sheet.py ./out --cols 4
# → ./out/contact.jpg（带文件名编号，一次看全）
```

## 脚本一览

| 脚本 | 说明 |
|---|---|
| `scripts/weibo_video.py` | 微博视频全档位下载。cookie 支持 `--session-json` / `--cookie` / `--cookie-file` / `WEIBO_COOKIE` |
| `scripts/threads_dl.py` | Threads 视频 + 图片。`--provider auto/page/mirror`，`--images` |
| `scripts/probe_mp4.py` | mp4 容器解析（宽高/时长/编码/轨道），零依赖 |
| `scripts/contact_sheet.py` | 图片目录 → 带编号对照图 |

## 实测记录

详细的接口字段、坑、以及「不要信哪些字段」都在 `references/`：

- [`references/weibo.md`](references/weibo.md) — 微博接口、登录墙触发条件、puppeteer 兜底
- [`references/threads.md`](references/threads.md) — 网络阻断实测、两条抓取路线、镜像服务细节

## 自测

```bash
python scripts/weibo_video.py --selftest    # 离线验证档位解析
python scripts/threads_dl.py --selftest     # 离线验证 URL 解析
```

两个 selftest 不联网，覆盖：档位排序、HTML 实体还原、480p 复用检测、零宽字符清理、
文件名安全化、视频 URL 去重。

## 已知限制

- 微博图文帖 / 老帖会撞登录墙（`ok:-100`），本脚本只处理视频帖。图文请走 puppeteer 拦 XHR。
- 微博视频号最高 1080p，不存在 4K 档。
- Threads 的 `--provider mirror` 依赖第三方服务，对方改版就会失效；届时用 `--provider page` + 代理。
- Threads 图片经镜像拿到的是 `e35` 档（最长边约 2139），不是绝对原图。
- 不支持批量爬账号时间线，单帖抓取。批量请自行控制频率。

## License

MIT
