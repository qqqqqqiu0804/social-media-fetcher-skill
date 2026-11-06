#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weibo_video.py — 下载微博视频的**最高画质**版本（单文件，无第三方依赖）。

用法:
    python weibo_video.py "https://weibo.com/7735668873/QgQGBDZ9t" -o ./out
    python weibo_video.py "<url>" --list                 # 只列档位，不下载
    python weibo_video.py "<url>" --q 720p -o ./out      # 指定档位
    python weibo_video.py "<url>" --cookie "SUB=...; SUBP=..."
    python weibo_video.py "<url>" --session-json weibo-exp/data/session.json
    python weibo_video.py --selftest                      # 离线验证解析逻辑

为什么不用现成工具:
  yt-dlp / you-get 这类通用工具对微博视频只能拿到 play_info 里那条 stream_url（多半是 720p），
  而真正的高画质在 page_info.media_info.playback_list[] 里，每档一条独立直链。
  这份 JSON 只有带登录 cookie 打 ajax 接口才给全 —— 浏览器里扒 DOM 反而只能看到当前播放档。

数据来源（两处，缺一不可）:
  1) https://weibo.com/ajax/statuses/show?id=<mid>&locale=zh-CN
     → page_info.media_info.playback_list[]，每项 { meta.quality_label, play_info.url/width/height/bitrate/size }
  2) 直链下载时 Referer 必须是 https://weibo.com/，否则 403。

坑（实测）:
  - 直链带 ssig + Expires（约 1 小时），过期重跑脚本即可，不用换链接。
  - 对直链发 HEAD 会 403，必须 GET（本脚本用流式 GET）。
  - 480p 档经常直接复用 720p 的码流（url 完全一致），别以为是抓错了。
  - 有的帖子 playback_list 里有 1080p 但 h265_mp4_hd 等字段为空，不代表有 4K 档；
    微博视频号目前最高就到 1080p。
"""

from __future__ import annotations

import argparse
import gzip
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MID_RE = re.compile(r"weibo\.(?:com|cn)/(?:\d+|detail|u/\d+)/([A-Za-z0-9]+)", re.I)
SHORT_RE = re.compile(r"weibo\.(?:com|cn)/([A-Za-z0-9]{9,12})\s*$", re.I)


# --------------------------------------------------------------------- http

def http_get(url: str, headers: dict[str, str], timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def http_get_json(url: str, headers: dict[str, str], timeout: int = 60):
    return json.loads(http_get(url, headers, timeout).decode("utf-8", "replace"))


def download_stream(src: str, dest: str, headers: dict[str, str], timeout: int = 120) -> int:
    """流式下载，带进度。注意：微博视频 CDN 对 HEAD 返回 403，只能 GET。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(src, headers=headers)
    started = time.time()
    got = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total:
                sys.stderr.write(f"\r  {got / 1048576:.1f}/{total / 1048576:.1f} MiB")
                sys.stderr.flush()
    os.replace(tmp, dest)
    speed = got / max(time.time() - started, 0.001) / 1048576
    sys.stderr.write(f"\r  完成 {got / 1048576:.1f} MiB ({speed:.1f} MiB/s)\n")
    return got


# ------------------------------------------------------------------ cookies

def parse_cookie_string(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_netscape_cookie_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 7 and "weibo" in f[0].lower():
                out[f[5]] = f[6]
    return out


def parse_weibo_exp_session(path: str) -> dict[str, str]:
    """解析 weibo-exp 风格的 session.json：{"cookies":[{name,value,domain,expires}]}。

    这是本机最常用的来源（weibo-exp 项目签到用的那份 cookie jar），
    按 Chromium 规则过滤过期项与不相关域。
    """
    data = json.loads(open(path, "r", encoding="utf-8").read())
    cookies = data.get("cookies") or []
    now = int(time.time())
    out: dict[str, str] = {}
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name:
            continue
        exp = c.get("expires") or c.get("expirationDate") or 0
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            exp = 0
        if exp > 0 and exp < now:
            continue
        dom = (c.get("domain") or "").lstrip(".")
        if not re.search(r"(^|\.)(weibo\.(com|cn)|sina\.com\.cn)$", dom):
            continue
        out[name] = value
    return out


def build_cookie_header(args) -> str:
    cookies: dict[str, str] = {}
    env = os.environ.get("WEIBO_COOKIE")
    if env:
        cookies.update(parse_cookie_string(env))
    if args.cookie:
        cookies.update(parse_cookie_string(args.cookie))
    if args.cookie_file:
        cookies.update(parse_netscape_cookie_file(args.cookie_file))
    if args.session_json:
        cookies.update(parse_weibo_exp_session(args.session_json))
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# -------------------------------------------------------------------- fetch

def resolve_mid(url: str) -> str:
    """从各种形态的微博链接里取 mid / mblogid。"""
    url = url.strip()
    m = MID_RE.search(url) or SHORT_RE.search(url)
    if not m:
        raise SystemExit(f"URL 里找不到微博 ID：{url}")
    return m.group(1)


def fetch_status(mid: str, cookie: str, timeout: int = 60, referer_uid: str = "") -> dict:
    api = f"https://weibo.com/ajax/statuses/show?id={mid}&locale=zh-CN"
    headers = {
        "Cookie": cookie,
        "User-Agent": UA,
        "Referer": f"https://weibo.com/{referer_uid}/{mid}" if referer_uid else "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "client-version": "v2.47.42",
        "server-version": "v2024.11.28.1",
    }
    try:
        return http_get_json(api, headers, timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        raise SystemExit(f"status/show HTTP {exc.code}：{body}\n（多半是 cookie 过期，重新登一次微博）") from exc


def extract_tiers(status: dict) -> list[dict]:
    """把 playback_list 里的可下载档位整理成统一结构，按分辨率降序。"""
    pi = status.get("page_info") or {}
    ml = pi.get("media_info") or {}
    tiers: list[dict] = []
    for item in ml.get("playback_list") or []:
        pinfo = item.get("play_info") or {}
        meta = item.get("meta") or {}
        url = pinfo.get("url")
        if not url or pinfo.get("type") != 1:
            continue
        if not str(url).lower().split("?")[0].endswith((".mp4", ".m4v")):
            continue
        tiers.append(
            {
                "label": meta.get("quality_label") or pinfo.get("quality_label") or "?",
                "desc": meta.get("quality_desc") or pinfo.get("quality_desc") or "",
                "url": html_lib.unescape(url),
                "width": pinfo.get("width") or 0,
                "height": pinfo.get("height") or 0,
                "bitrate": pinfo.get("bitrate") or 0,
                "size": pinfo.get("size") or 0,
                "duration": pinfo.get("duration") or 0,
                "codec": pinfo.get("video_codecs") or "",
            }
        )
    # 去重：480p 常复用 720p 码流
    seen: set[str] = set()
    uniq: list[dict] = []
    for t in tiers:
        key = t["url"].split("?")[0]
        if key in seen:
            t["dup_of"] = next((u["label"] for u in uniq if u["url"].split("?")[0] == key), "")
            uniq.append(t)
            continue
        seen.add(key)
        uniq.append(t)
    uniq.sort(key=lambda t: (t["height"], t["bitrate"]), reverse=True)
    return uniq


def clean_text(raw: str) -> str:
    return re.sub(r"[\u200b-\u200d\ufeff]", "", raw or "").strip()


def safe_name(s: str, limit: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", s)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s[:limit] or "weibo-video"


# ---------------------------------------------------------------- selftest

FIXTURE = {
    "id": "5240157779529863",
    "mblogid": "QgQGBDZ9t",
    "created_at": "Thu Dec 04 15:47:15 +0800 2025",
    "text_raw": "测试正文\u200b #话题# http://t.cn/xxx",
    "user": {"screen_name": "测试作者"},
    "page_info": {
        "type": 11,
        "media_info": {
            "media_id": "5240156723282033",
            "duration": 450,
            "author_name": "测试作者",
            "playback_list": [
                {"meta": {"quality_label": "1080p", "quality_desc": "高清"},
                 "play_info": {"type": 1, "url": "https://f.video.weibocdn.com/o0/a.mp4?label=mp4_1080p&amp;ssig=x",
                               "width": 1920, "height": 1080, "bitrate": 1494746,
                               "size": 84231954, "duration": 450.816, "video_codecs": "avc1.640032"}},
                {"meta": {"quality_label": "720p"},
                 "play_info": {"type": 1, "url": "https://f.video.weibocdn.com/o0/b.mp4?label=mp4_720p",
                               "width": 1280, "height": 720, "bitrate": 761310, "size": 42901355}},
                {"meta": {"quality_label": "480p"},
                 "play_info": {"type": 1, "url": "https://f.video.weibocdn.com/o0/b.mp4?label=mp4_720p",
                               "width": 1280, "height": 720, "bitrate": 761310, "size": 42901355}},
                {"meta": {"quality_label": None},
                 "play_info": {"type": 3, "url": "https://wx3.sinaimg.cn/large/scrub.jpg"}},
            ],
        },
    },
}


def selftest() -> int:
    ok = True
    tiers = extract_tiers(FIXTURE)

    print(f"档位数: {len(tiers)}")
    for t in tiers:
        print(f"  {t['label']:8s} {t['width']}x{t['height']}  {t['bitrate']:>8} bps  dup={t.get('dup_of', '')}")

    if len(tiers) != 3:
        print("FAIL: 应筛出 3 档（图片那张 type=3 要排除）")
        ok = False
    if tiers[0]["label"] != "1080p":
        print("FAIL: 未按分辨率降序")
        ok = False
    if "&amp;" in tiers[0]["url"]:
        print("FAIL: HTML 实体未还原（会成为 403 的根源）")
        ok = False
    if not tiers[0]["url"].endswith("&ssig=x"):
        print("FAIL: URL 反转义不完整")
        ok = False
    dup = [t for t in tiers if t.get("dup_of")]
    if not dup or dup[0]["label"] != "480p":
        print("FAIL: 480p 复用 720p 码流未被标记")
        ok = False
    if clean_text(FIXTURE["text_raw"]) != "测试正文 #话题# http://t.cn/xxx":
        print("FAIL: 零宽字符没清干净")
        ok = False
    if safe_name('a/b:c*?"<>|d') != "a-b-c-d":
        print("FAIL: 文件名安全化不正确")
        ok = False
    if resolve_mid("https://weibo.com/7735668873/QgQGBDZ9t?layerid=1") != "QgQGBDZ9t":
        print("FAIL: mid 提取失败")
        ok = False

    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="下载微博视频最高画质")
    ap.add_argument("url", nargs="?", help="微博链接（weibo.com/<uid>/<mid> 或 m.weibo.cn/detail/<id>）")
    ap.add_argument("-o", "--out", default="downloads", help="保存目录（默认 downloads）")
    ap.add_argument("--q", default="1080p", help="目标档位：1080p / 720p / 480p，默认 1080p")
    ap.add_argument("--list", action="store_true", help="只列档位，不下载")
    ap.add_argument("--cookie", help='整串 cookie："SUB=...; SUBP=..."')
    ap.add_argument("--cookie-file", help="Netscape 格式 cookies.txt")
    ap.add_argument("--session-json", help="weibo-exp 风格的 session.json（带 cookies 数组）")
    ap.add_argument("--name", help="输出文件名（不含扩展名）；默认 <mid>-<档位>")
    ap.add_argument("--timeout", type=int, default=60, help="单次请求超时秒数（默认 60）")
    ap.add_argument("--selftest", action="store_true", help="离线验证解析逻辑")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.url:
        ap.error("需要一个微博链接（或 --selftest）")

    cookie = build_cookie_header(args)
    if not cookie:
        print("[!] 没带 cookie：接口大概率返回登录墙 / 空 playback_list")
        print("    传 --session-json <weibo-exp session.json> 或 --cookie \"SUB=...\"")

    mid = resolve_mid(args.url)
    uid_m = re.search(r"weibo\.com/(\d+)/", args.url)
    print(f"[*] mid: {mid}")

    status = fetch_status(mid, cookie, args.timeout, uid_m.group(1) if uid_m else "")
    if not status.get("id"):
        raise SystemExit(f"接口没返回微博：{str(status)[:300]}")

    pi = status.get("page_info") or {}
    if int(pi.get("type") or 0) != 11:
        raise SystemExit(
            f"这条不是视频微博（page_info.type = {pi.get('type')}）。"
            "图文帖请用 weibo_media.mjs（需要 puppeteer）。"
        )

    tiers = extract_tiers(status)
    if not tiers:
        raise SystemExit("playback_list 里没有可下载的 mp4（可能需要登录 cookie）")

    print("\n可选档位：")
    for t in tiers:
        extra = f"  (复用 {t['dup_of']} 码流)" if t.get("dup_of") else ""
        size = f"{t['size'] / 1048576:.1f} MiB" if t["size"] else "?"
        print(f"  {t['label']:8s} {t['width']}x{t['height']}  {t['bitrate'] / 1000:6.0f} kb/s  {size}{extra}")

    if args.list:
        return 0

    pick = next((t for t in tiers if t["label"] == args.q), None)
    if pick is None:
        print(f"[!] 没有 {args.q} 档，回退 {tiers[0]['label']}")
        pick = tiers[0]
    if pick.get("dup_of"):
        print(f"[!] 注意：{pick['label']} 与 {pick['dup_of']} 是同一份码流")

    ml = pi.get("media_info") or {}
    text = clean_text(status.get("text_raw") or "")
    stem = args.name or f"{mid}-{pick['label']}"
    dst = os.path.join(args.out, f"{safe_name(stem)}.mp4")

    print(f"\n下载 {pick['label']} ({pick['width']}x{pick['height']})")
    print(f"  → {dst}")
    if os.path.exists(dst) and os.path.getsize(dst) > 100_000:
        print(f"  已存在，跳过（{os.path.getsize(dst) / 1048576:.1f} MiB）")
    else:
        try:
            download_stream(pick["url"], dst, {
                "User-Agent": UA,
                "Referer": "https://weibo.com/",
                "Accept": "*/*",
            }, args.timeout)
        except urllib.error.HTTPError as exc:
            raise SystemExit(
                f"直链 HTTP {exc.code}：ssig 过期了，重跑本脚本重新取链接即可"
            ) from exc

    # 封面
    cover = (((ml.get("big_pic_info") or {}).get("pic_big")) or {}).get("url")
    if cover:
        try:
            data = http_get(cover, {"User-Agent": UA, "Referer": "https://weibo.com/"})
            open(os.path.join(args.out, "cover.jpg"), "wb").write(data)
        except Exception:  # noqa: BLE001
            pass

    meta = {
        "url": args.url,
        "mid": mid,
        "mblogid": status.get("mblogid"),
        "created_at": status.get("created_at"),
        "author": (status.get("user") or {}).get("screen_name") or ml.get("author_name"),
        "text": text,
        "media_id": ml.get("media_id"),
        "duration_s": ml.get("duration"),
        "picked": {k: pick[k] for k in ("label", "width", "height", "bitrate", "size", "codec")},
        "tiers": [
            {"label": t["label"], "res": f"{t['width']}x{t['height']}", "bitrate": t["bitrate"],
             "size": t["size"], "dup_of": t.get("dup_of", "")}
            for t in tiers
        ],
        "file": os.path.basename(dst),
    }
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    print(f"\n[✓] {dst}")
    if text:
        print(f"    正文：{text[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
