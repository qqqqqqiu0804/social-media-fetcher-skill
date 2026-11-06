#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contact_sheet.py — 把一个目录里的图片铺成一张带编号的对照图。

为什么需要：一条帖子十几张图，逐张打开要点十几次、还占上下文。
拼成一张带编号的 contact sheet，一次就能看全，编号直接对应文件名。

用法:
    python contact_sheet.py ./out            # → ./out/contact.jpg
    python contact_sheet.py ./out --cols 4 --cell 320 --out D:/tmp/sheet.jpg

依赖 Pillow。没有 Pillow 时会退化为「只列清单」模式，不报错。
"""

from __future__ import annotations

import argparse
import math
import os
import sys

EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def list_images(d: str) -> list[str]:
    return sorted(
        f for f in os.listdir(d)
        if f.lower().endswith(EXTS) and os.path.isfile(os.path.join(d, f))
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="把图片目录铺成带编号对照图")
    ap.add_argument("dir", help="图片目录")
    ap.add_argument("--cols", type=int, default=4, help="每行几张（默认 4）")
    ap.add_argument("--cell", type=int, default=300, help="单格像素（默认 300）")
    ap.add_argument("--out", help="输出路径，默认 <dir>/contact.jpg")
    ap.add_argument("--skip-contact", action="store_true", help="不把已有的 contact.jpg 算进去")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        raise SystemExit(f"目录不存在：{args.dir}")

    files = list_images(args.dir)
    if args.skip_contact:
        files = [f for f in files if f != "contact.jpg"]
    if not files:
        print("没有图片文件")
        return 1

    out = args.out or os.path.join(os.path.abspath(args.dir), "contact.jpg")
    out = os.path.abspath(out)

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("没装 Pillow，只能列清单：")
        for f in files:
            print("  ", f)
        return 0

    cells = []
    for f in files:
        try:
            im = Image.open(os.path.join(args.dir, f)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过 {f}：{exc}")
            continue
        im.thumbnail((args.cell - 10, args.cell - 10), Image.LANCZOS)
        cells.append((f, im))

    if not cells:
        print("没有可读图片")
        return 1

    cols = max(1, args.cols)
    rows = math.ceil(len(cells) / cols)
    cw = max(c[1].width for c in cells) + 10
    ch = max(c[1].height for c in cells) + 30
    sheet = Image.new("RGB", (cols * cw, rows * ch), (22, 22, 26))
    dr = ImageDraw.Draw(sheet)

    for i, (name, im) in enumerate(cells):
        r, c = divmod(i, cols)
        x0, y0 = c * cw, r * ch
        bw = min(len(name) * 7 + 10, cw - 4)
        dr.rectangle([x0 + 2, y0 + 2, x0 + 2 + bw, y0 + 18], fill=(255, 214, 10))
        dr.text((x0 + 6, y0 + 4), name, fill=(0, 0, 0))
        sheet.paste(im, (x0 + (cw - im.width) // 2, y0 + 22 + (ch - 30 - im.height) // 2))

    sheet.save(out, "JPEG", quality=88)
    print(f"OK {len(cells)} 张 -> {out} {sheet.size[0]}x{sheet.size[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
