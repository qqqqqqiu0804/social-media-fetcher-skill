#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_mp4.py — 不装 ffprobe 也能读 mp4 的规格，用来自证「下到的真的是这一档」。

为什么需要：下载工具说 1080p 不算数。要证明文件里真的是 1920x1080、时长对得上、
不是被 CDN 降档的伪高清，就得解析 moov/trak/tkhd/stsd。

用法:
    python probe_mp4.py ./out/video.mp4
    python probe_mp4.py ./out/                 # 扫目录下所有 mp4
    python probe_mp4.py ./out --frame 200      # 顺便用 ffmpeg 抽第 200 秒一帧（需 imageio-ffmpeg）

零依赖（纯 struct 解析）。--frame 才需要 ffmpeg。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}


def boxes(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        typ = buf[i + 4:i + 8]
        hdr = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            return
        yield typ, i + hdr, i + size
        i += size


def find_path(buf: bytes, path: list[bytes]):
    cur = [(0, len(buf))]
    for name in path:
        nxt = []
        for s, e in cur:
            for typ, bs, be in boxes(buf, s, e):
                if typ == name:
                    nxt.append((bs, be))
        cur = nxt
        if not cur:
            return None
    return cur[0] if cur else None


def parse_hands(buf: bytes, mvhd: tuple[int, int]) -> float:
    s, _ = mvhd
    version = buf[s]
    if version == 0:
        ts, dur = struct.unpack(">II", buf[s + 12:s + 20])
    else:
        ts, dur = struct.unpack(">IQ", buf[s + 20:s + 32])
    return dur / ts if ts else 0.0


def handler_of(buf: bytes, trak: tuple[int, int]) -> str:
    r = find_path(buf, [b"mdia", b"hdlr"])
    if not r:
        return "?"
    s, _ = r
    return buf[s + 8:s + 12].decode("latin1", "replace")


def probe(path: str, frame_at: float | None = None) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(32)
        # 只读 moov：先扫顶层 box 表，再定位 moov
        fh.seek(0)
        top = []
        while True:
            pos = fh.tell()
            h = fh.read(8)
            if len(h) < 8:
                break
            bs = struct.unpack(">I", h[:4])[0]
            typ = h[4:8]
            hdr = 8
            if bs == 1:
                bs = struct.unpack(">Q", fh.read(8))[0]
                hdr = 16
            elif bs == 0:
                fh.seek(0, os.SEEK_END)
                bs = fh.tell() - pos
            if bs < hdr:
                break
            top.append((typ, pos, bs, hdr))
            fh.seek(pos + bs)
        moov = next((t for t in top if t[0] == b"moov"), None)
        if not moov:
            raise ValueError("没有 moov（不是有效的 mp4，或者文件被截断）")
        _typ, mpos, msize, mhdr = moov
        fh.seek(mpos + mhdr)
        buf = fh.read(msize - mhdr)

    mvhd = find_path(buf, [b"mvhd"])
    duration = parse_hands(buf, mvhd) if mvhd else 0.0

    traks = [t for t in boxes(buf, 0, len(buf)) if t[0] == b"trak"]
    tracks = []
    for _t, ts_, te_ in traks:
        kind = "?"
        for typ, bs, be in boxes(buf, ts_, te_):
            if typ == b"mdia":
                for t2, b2, e2 in boxes(buf, bs, be):
                    if t2 == b"hdlr":
                        kind = buf[b2 + 8:b2 + 12].decode("latin1", "replace")
        tracks.append(kind)

    # 只取视频轨（tkhd / stsd 都嵌在 trak 里，不能只扫顶层）
    vide = None
    for idx, (_t, ts_, te_) in enumerate(traks):
        if idx < len(tracks) and tracks[idx] == "vide":
            vide = (ts_, te_)
            break
    if vide is None and traks:
        vide = (traks[0][1], traks[0][2])

    w = h = 0
    codec = ""
    if vide:
        tkhd = None
        for typ, bs, be in boxes(buf, vide[0], vide[1]):
            if typ == b"tkhd":
                tkhd = (bs, be)
                break
        if tkhd:
            s, _ = tkhd
            version = buf[s]
            off = s + (76 if version == 0 else 88)
            if off + 8 <= len(buf):
                w = struct.unpack(">I", buf[off:off + 4])[0] / 65536
                h = struct.unpack(">I", buf[off + 4:off + 8])[0] / 65536

        stsd = None
        for typ, bs, be in boxes(buf, vide[0], vide[1]):
            if typ != b"mdia":
                continue
            for t2, b2, e2 in boxes(buf, bs, be):
                if t2 != b"minf":
                    continue
                for t3, b3, e3 in boxes(buf, b2, e2):
                    if t3 != b"stbl":
                        continue
                    for t4, b4, e4 in boxes(buf, b3, e3):
                        if t4 == b"stsd":
                            stsd = (b4, e4)
        if stsd:
            s, _ = stsd
            e = s + 8
            if e + 8 <= len(buf):
                codec = buf[e + 4:e + 8].decode("latin1", "replace")
                if codec in ("avc1", "hvc1", "hev1") and e + 36 <= len(buf):
                    sw = struct.unpack(">H", buf[e + 32:e + 34])[0]
                    sh = struct.unpack(">H", buf[e + 34:e + 36])[0]
                    w = sw or w
                    h = sh or h

    out = {
        "file": path,
        "bytes": size,
        "duration_s": round(duration, 3),
        "tracks": tracks,
        "width": int(w),
        "height": int(h),
        "codec": codec,
        "top_boxes": [t[0].decode("latin1", "replace") for t in top],
    }

    if frame_at is not None:
        try:
            import subprocess
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            dst = os.path.splitext(path)[0] + f"-frame{int(frame_at)}s.jpg"
            subprocess.run(
                [exe, "-y", "-loglevel", "error", "-ss", str(frame_at), "-i", path,
                 "-frames:v", "1", "-q:v", "3", dst],
                check=True, capture_output=True,
            )
            out["frame"] = dst
        except Exception as exc:  # noqa: BLE001
            out["frame_error"] = f"{type(exc).__name__}: {exc}"

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="解析 mp4 规格（零依赖）")
    ap.add_argument("path", help="mp4 文件或目录")
    ap.add_argument("--frame", type=float, help="顺便抽这一秒的画面（需 imageio-ffmpeg）")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        raise SystemExit(f"路径不存在：{args.path}")

    targets: list[str] = []
    if os.path.isdir(args.path):
        targets = [os.path.join(args.path, f) for f in sorted(os.listdir(args.path))
                   if f.lower().endswith((".mp4", ".m4v"))]
    else:
        targets = [args.path]

    if not targets:
        print("没有 mp4 文件")
        return 1

    rc = 0
    for i, t in enumerate(targets):
        print("=" * 62)
        try:
            r = probe(t, args.frame if len(targets) == 1 else None)
        except Exception as exc:  # noqa: BLE001
            print(f"{os.path.basename(t)}: FAIL {type(exc).__name__}: {exc}")
            rc = 1
            continue
        print(f"文件      {os.path.basename(r['file'])}")
        print(f"体积      {r['bytes']:,} B ({r['bytes'] / 1048576:.2f} MiB)")
        print(f"时长      {r['duration_s']} s")
        print(f"分辨率    {r['width']}x{r['height']}")
        print(f"视频编码  {r['codec']}")
        print(f"轨道      {', '.join(r['tracks'])}")
        print(f"顶层 box  {', '.join(r['top_boxes'])}")
        if r.get("frame"):
            print(f"抽帧      {r['frame']}")
        if r.get("frame_error"):
            print(f"抽帧失败  {r['frame_error']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
