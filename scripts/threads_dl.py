#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""threads_dl.py — 下载 Threads 帖子里的视频 / 图片（单文件，无第三方依赖）。

用法:
    python threads_dl.py "https://www.threads.com/@user/post/XXXX" -o ./out
    python threads_dl.py "https://www.threads.com/@user/post/XXXX" --cookie "sessionid=abc; ds_user_id=123"
    python threads_dl.py "https://www.threads.com/@user/post/XXXX" --cookie-file cookies.txt
    python threads_dl.py "https://www.threads.com/@user/post/XXXX" --cookies-from-browser chrome
    python threads_dl.py "https://www.threads.com/@user/post/XXXX" --provider mirror --images
    python threads_dl.py --selftest          # 用内置假页面验证解析逻辑，不联网

两条抓取路线（--provider auto 时按序降级）:

  page   —— 自己抓 threads.com 页面
    1) /embed 页面：公开嵌入页通常无需登录，页面里直接内联 video_versions / image_versions2
    2) 正帖页面 + 浏览器式导航头（Sec-Fetch-*）：匿名常拿到空壳，带 sessionid 才出 JSON
    3) og:video / og:image 兜底正则
    Threads 在页面里把帖子 JSON 转义后内联（\\/ 之类），所以先做反转义再按 "video_versions": [ 定位，
    用括号配平切出 JSON 数组解析，避免整页 JSON 结构变动就挂。

  mirror —— 走 threadsphotodownloader.com 的公开服务代解析（本机直连 Meta 全线被墙时唯一可行）
    1) GET / 拿 Rails 会话 cookie 与 authenticity_token
    2) POST /download {url} → 302 到 /download/<job_id>
    3) 该页内嵌 turbo-frame src="/download/<job_id>/status"，轮询拿结果分片
    4) 结果片里是 <a download href="https://cdn.threadsphotodownloader.com/<origin-host>/<path>">，
       即对方 CDN 反向代理；把首段域名去掉就是 Meta 原始 CDN 地址。
    5) 下载走镜像前缀（本机到 scontent/fbcdn 的 443 不通），Referer 必须带对方站点。

  实测（2026-11-06）: 直连 threads.com / scontent.cdninstagram.com 的 443 全超时或 TLS reset，
  DNS 还返回被污染的 IP（104.244.46.85 / 162.125.32.x）。mirror 路线可稳定拿到 9 图 + 1 视频。
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

NAV_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# --------------------------------------------------------------------- http

def http_get(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    opener = _OPENER or urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


_OPENER = None


def set_proxy(proxy: str | None) -> None:
    """proxy 形如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080（socks 需 PySocks）。"""
    global _OPENER
    if not proxy:
        _OPENER = None
        return
    _OPENER = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    print(f"[*] 走代理: {proxy}")


def build_headers(cookies: dict[str, str]) -> dict[str, str]:
    headers = dict(NAV_HEADERS)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers


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
    """解析 Chrome/Firefox 扩展导出的 cookies.txt。"""
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7 and "thread" in fields[0].lower():
                out[fields[5]] = fields[6]
    return out


def cookies_from_browser(name: str) -> dict[str, str]:
    """借 yt-dlp 的 cookie 提取器读本机浏览器登录态（可选依赖）。"""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("--cookies-from-browser 需要先 pip install yt-dlp") from exc

    jar = extract_cookies_from_browser(name)
    return {
        c.name: c.value
        for c in jar
        if "threads" in (c.domain or "") or "instagram" in (c.domain or "")
    }


# ------------------------------------------------------------------- parse

def unescape_page(html: str) -> str:
    """反转义 inlined JSON：把 \\/ 变回 /，但保留真正的 \\" 转义。"""
    return html.replace('\\"', "\x00").replace("\\/", "/").replace("\x00", '\\"')


def json_at(text: str, start: int):
    """从 start 处的 [ 或 { 起按括号配平切出完整 JSON 字面量。"""
    opener = text[start]
    closer = {"[": "]", "{": "}"}[opener]
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON literal")


def decode_json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def clean_url(u: str) -> str:
    """URL 可能连着 HTML 实体（&amp;）或 JSON 转义，统一还原。"""
    return html_lib.unescape(u.replace("\\/", "/"))


def extract_media(html: str) -> dict:
    text = unescape_page(html)

    videos: list[str] = []
    for m in re.finditer(r'"video_versions":\s*(\[)', text):
        try:
            entries = json_at(text, m.start(1))
        except (ValueError, json.JSONDecodeError):
            continue
        for e in entries:
            url = (e or {}).get("url")
            if url:
                url = clean_url(url)
            if url and url not in videos:
                videos.append(url)

    images: list[tuple[int, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'"image_versions2":\s*(\{)', text):
        try:
            obj = json_at(text, m.start(1))
        except (ValueError, json.JSONDecodeError):
            continue
        for c in obj.get("candidates") or []:
            url = c.get("url")
            if url:
                url = clean_url(url)
            if url and url not in seen:
                seen.add(url)
                images.append((c.get("width") or 0, url))
    images.sort(key=lambda p: p[0], reverse=True)

    # og:* 兜底
    if not videos:
        for m in re.finditer(r'<meta[^>]+property="og:video"[^>]+content="([^"]+)"', html):
            u = clean_url(m.group(1))
            if u not in videos:
                videos.append(u)
    if not images:
        for m in re.finditer(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html):
            images.append((0, clean_url(m.group(1))))

    caption = None
    m = re.search(r'"caption":\s*\{.*?"text":\s*"(.*?)(?<!\\)"', text, re.DOTALL)
    if m:
        caption = decode_json_string(m.group(1)).strip() or None

    return {
        "videos": videos,
        "images": [u for _, u in images],
        "thumbnail": images[0][1] if images else None,
        "caption": caption,
    }


# -------------------------------------------------------------------- urls

POST_RE = re.compile(
    r"threads\.(?:com|net)/(?:@[\w.]+/post/|t/)([\w-]+)", re.IGNORECASE
)


def post_variants(url: str) -> list[str]:
    """正帖页 + embed 页 + threads.net 等价页，全都试一遍。"""
    url = url.strip().split("?")[0].rstrip("/")
    out = [url + "/embed", url]
    m = POST_RE.search(url)
    if m:
        code = m.group(1)
        base = re.sub(r"threads\.(?:com|net)", "threads.com", url, flags=re.I)
        out.append(base + "/embed")
        out.append(f"https://www.threads.net/t/{code}/embed")
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def fetch_media(url: str, cookies: dict[str, str], timeout: int = 30) -> tuple[dict, str]:
    headers = build_headers(cookies)
    last_err = None
    for candidate in post_variants(url):
        try:
            html = http_get(candidate, headers, timeout).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_err = f"{candidate} -> HTTP {exc.code}"
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = f"{candidate} -> {type(exc).__name__}: {exc}"
            continue
        media = extract_media(html)
        if media["videos"] or media["images"]:
            return media, candidate
        last_err = f"{candidate} -> 页面里没有媒体（可能是空壳/已删除/仅文字）"
    raise SystemExit(f"全部尝试失败：{last_err}")


# ------------------------------------------------------------------ mirror

MIRROR_BASE = "https://threadsphotodownloader.com"
MIRROR_CDN = "https://cdn.threadsphotodownloader.com"


def mirror_resolve(
    url: str, timeout: int = 60, poll: int = 15, interval: float = 2.0
) -> dict:
    """走 threadsphotodownloader.com 代解析，返回 {videos, images, proxied, caption, ...}。

    proxied 为 True 说明 URL 已经过对方 CDN 反代（本机直连 Meta 不通时按这个下）。
    """
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base_headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }

    def fetch(req_url, extra=None, data=None):
        hd = dict(base_headers)
        hd.update(extra or {})
        req = urllib.request.Request(req_url, headers=hd, data=data)
        with opener.open(req, timeout=timeout) as resp:
            return resp.geturl(), resp.read().decode("utf-8", "replace")

    home_url, home = fetch(MIRROR_BASE + "/")
    m = re.search(r'name="authenticity_token" value="([^"]+)"', home)
    if not m:
        raise SystemExit("mirror: 首页拿不到 authenticity_token（对方改版？）")
    token = m.group(1)

    body = urllib.parse.urlencode({"authenticity_token": token, "url": url}).encode()
    final_url, page = fetch(
        MIRROR_BASE + "/download",
        {"Content-Type": "application/x-www-form-urlencoded", "Origin": MIRROR_BASE,
         "Referer": MIRROR_BASE + "/"},
        data=body,
    )

    job = re.search(r"/download/([A-Za-z0-9_\-]+)", final_url)
    if not job:
        raise SystemExit("mirror: POST 之后没拿到 job id（链接可能无效/非公开）")
    job_id = job.group(1)

    frame_src = re.search(r'<turbo-frame[^>]+src="([^"]*status)"', page)
    status_path = frame_src.group(1) if frame_src else f"/download/{job_id}/status"

    html = ""
    for _ in range(poll):
        _, html = fetch(
            MIRROR_BASE + status_path,
            {"Accept": "text/vnd.turbo-stream.html, text/html, */*",
             "Turbo-Frame": "media_resolution",
             "Referer": final_url},
        )
        if re.search(r"https?://[^\"'\s<>]*(?:cdninstagram|fbcdn)", html):
            break
        time.sleep(interval)
    else:
        raise SystemExit("mirror: 轮询结束仍未拿到媒体（帖子可能只有文字或已被限制）")

    raw_urls = re.findall(r'href="(https?://[^"]+)"', html)
    videos: list[str] = []
    images: list[str] = []
    originals: list[str] = []
    for raw in raw_urls:
        u = html_lib.unescape(raw)
        if not any(k in u for k in ("cdninstagram", "fbcdn", ".mp4")):
            continue
        orig = u
        if u.startswith(MIRROR_CDN + "/"):
            orig = "https://" + u[len(MIRROR_CDN) + 1:]
        if u not in videos and u not in images:
            (videos if ".mp4" in u.lower() else images).append(u)
            originals.append(orig)

    return {
        "videos": videos,
        "images": images,
        "originals": originals,
        "thumbnail": images[0] if images else None,
        "caption": None,
        "job_id": job_id,
        "proxied": True,
        "page": final_url,
    }



def download(src: str, dest_dir: str, stem: str, ext: str, referer: str | None = None) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{stem}.{ext}")
    tmp = dest + ".part"
    hd = {"User-Agent": UA}
    if referer:
        hd["Referer"] = referer
    req = urllib.request.Request(src, headers=hd)
    started = time.time()
    opener = _OPENER or urllib.request.build_opener()
    with opener.open(req, timeout=60) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total:
                sys.stderr.write(f"\r  {got/1048576:.1f}/{total/1048576:.1f} MiB")
                sys.stderr.flush()
    os.replace(tmp, dest)
    speed = got / max(time.time() - started, 0.001) / 1048576
    sys.stderr.write(f"\r  完成 {got/1048576:.1f} MiB  ({speed:.1f} MiB/s)\n")
    return dest


def safe_stem(handle: str | None, code: str) -> str:
    h = (handle or "post").lstrip("@")
    h = re.sub(r'[\\/:*?"<>|\s]+', "_", h)[:40]
    return f"threads-{h}-{code}"


# ---------------------------------------------------------------- selftest

FIXTURE = r'''
<html><head>
<meta property="og:image" content="https://scontent.cdninstagram.com/v/t51/thumb.jpg?stp=dst-jpg" />
</head><body><script>
{"thread_items":[{"post":{"code":"DfTnNxHmt7-","caption":{"text":"测试 caption \u4e2d\u6587" },
"image_versions2":{"candidates":[{"width":1080,"url":"https:\/\/scontent.cdninstagram.com\/v\/big.jpg?a=1&amp;b=2"},{"width":480,"url":"https:\/\/scontent.cdninstagram.com\/v\/small.jpg"}]},
"video_versions":[{"type":101,"url":"https:\/\/scontent.cdninstagram.com\/o1\/v\/t16\/abc_n.mp4?_nc_cat=1&amp;efg=3"},{"type":102,"url":"https:\/\/scontent.cdninstagram.com\/o1\/v\/t16\/abc_n.mp4?_nc_cat=1&amp;efg=3"}]}}]}
</script></body></html>
'''


def selftest() -> int:
    media = extract_media(FIXTURE)
    ok = True
    print("videos:", media["videos"])
    print("images:", media["images"])
    print("caption:", media["caption"])
    print("thumbnail:", media["thumbnail"])
    if len(media["videos"]) != 1:
        print("FAIL: 视频应去重为 1 条")
        ok = False
    if not media["videos"] or not media["videos"][0].endswith("abc_n.mp4?_nc_cat=1&efg=3"):
        print("FAIL: 视频 URL 反转义不正确")
        ok = False
    if len(media["images"]) != 2 or media["images"][0].find("big.jpg") < 0:
        print("FAIL: 图片未按宽度降序")
        ok = False
    if media["caption"] != "测试 caption 中文":
        print("FAIL: caption 解码不正确")
        ok = False
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="下载 Threads 帖子视频/图片")
    ap.add_argument("url", nargs="?", help="Threads 帖子链接")
    ap.add_argument("-o", "--out", default="downloads", help="保存目录（默认 downloads）")
    ap.add_argument("--cookie", help='浏览器里复制的一整串 cookie，如 "sessionid=...; ds_user_id=..."')
    ap.add_argument("--cookie-file", help="Netscape 格式 cookies.txt")
    ap.add_argument("--cookies-from-browser", help="chrome / edge / firefox / brave")
    ap.add_argument("--images", action="store_true", help="图片也一起下（默认只下视频）")
    ap.add_argument("--proxy", help="代理，如 http://127.0.0.1:7890（访问 threads 常需要）")
    ap.add_argument("--provider", choices=["auto", "page", "mirror"], default="auto",
                    help="auto=先自己抓页面，失败再走镜像服务（默认）；page=只自己抓；mirror=只走镜像")
    ap.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数（默认 30）")
    ap.add_argument("--json", action="store_true", help="只打印解析出的媒体 URL（JSON），不下载")
    ap.add_argument("--selftest", action="store_true", help="用内置假页面验证解析逻辑")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.url:
        ap.error("需要一个 Threads 帖子链接（或 --selftest）")

    proxy = args.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    set_proxy(proxy)

    cookies: dict[str, str] = {}
    if args.cookie:
        cookies.update(parse_cookie_string(args.cookie))
    if args.cookie_file:
        cookies.update(parse_netscape_cookie_file(args.cookie_file))
    if args.cookies_from_browser:
        cookies.update(cookies_from_browser(args.cookies_from_browser))

    print(f"[*] cookie 数: {len(cookies)}（0 也能试公开 embed 页）")

    used = ""
    referer = None
    if args.provider in ("auto", "page"):
        try:
            media, used = fetch_media(args.url, cookies, args.timeout)
        except SystemExit as exc:
            if args.provider == "page":
                raise
            print(f"[!] 直抓失败，转镜像：{exc}")
            media, used = None, ""
            referer = MIRROR_BASE + "/"
    else:
        media, used = None, ""

    if media is None:
        media = mirror_resolve(args.url, timeout=max(args.timeout, 45))
        used = media.get("page") or "mirror"
        referer = MIRROR_BASE + "/"
        print(f"[*] mirror job: {media.get('job_id')}")

    print(f"[*] 命中来源: {used}")
    print(f"[*] 视频 {len(media['videos'])} 条 / 图片 {len(media['images'])} 张")
    if media.get("caption"):
        print(f"[*] caption: {media['caption'][:80]}")
    if media.get("originals") and media.get("proxied"):
        print("[*] 以下为 Meta 原始 CDN 地址（本机直连不通时请用镜像前缀下载）：")
        for o in media["originals"][:2]:
            print(f"    {o[:110]}...")

    if args.json:
        print(json.dumps(media, ensure_ascii=False, indent=2))
        return 0

    m = POST_RE.search(args.url)
    code = m.group(1) if m else str(int(time.time()))
    hm = re.search(r"threads\.(?:com|net)/@([\w.]+)/", args.url, re.I)
    stem = safe_stem(hm.group(1) if hm else None, code)

    saved = []
    for i, v in enumerate(media["videos"]):
        suffix = "" if i == 0 else f"-{i+1}"
        saved.append(download(v, args.out, stem + suffix, "mp4", referer))
    if args.images:
        for i, u in enumerate(media["images"]):
            ext = "jpg"
            mm = re.search(r"\.(jpg|jpeg|png|webp|heic)", u, re.I)
            if mm:
                ext = mm.group(1).lower()
            saved.append(download(u, args.out, f"{stem}-p{i+1}", ext, referer))

    if not saved:
        print("[!] 这个帖子没有视频（可能只有图片）；加 --images 再跑一次")
        return 2
    for p in saved:
        print("[✓]", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
