#!/usr/bin/env python3
"""Verify a demo recording or final cut.

    python check_video.py VIDEO                                # duration, size, resolution
    python check_video.py VIDEO --scan 20                      # find shrunken / grey-padded frames
    python check_video.py VIDEO --sheet sheet.png --every 8    # contact sheet (frame every 8 s)
    python check_video.py VIDEO --frame 100.3 --out frame.png  # one frame at a time position

Shrunken frames: if the browser window is moved, resized or minimized mid-take, Chromium
draws the page smaller inside a grey (128,128,128) canvas. --scan reports those times.
Needs ffmpeg/ffprobe on PATH and Pillow (`pip install pillow`) for --scan.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import subprocess
import sys


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration,size",
                          "-of", "json", path], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    stream = next(s for s in data["streams"] if "width" in s)
    return float(data["format"]["duration"]), int(data["format"]["size"]), stream["width"], stream["height"]


def frame_png(path, t, width=None) -> bytes:
    vf = ["-vf", f"scale={width}:-1"] if width else []
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", *vf,
                           "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout


def scan(path, every, duration):
    from PIL import Image, ImageChops

    bad = []
    t = 1.0
    while t < duration - 0.5:
        img = Image.open(io.BytesIO(frame_png(path, t, 360))).convert("RGB")
        diff = ImageChops.difference(img, Image.new("RGB", img.size, (128, 128, 128))).convert("L")
        box = diff.point(lambda v: 255 if v > 6 else 0).getbbox()  # content that isn't padding grey
        if box and (box[2] < img.width * 0.98 or box[3] < img.height * 0.98):
            bad.append(round(t, 1))
        t += every
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--scan", type=float, metavar="EVERY_S", help="check one frame every N seconds")
    ap.add_argument("--sheet", help="write a contact sheet PNG")
    ap.add_argument("--every", type=float, default=8, help="seconds between contact-sheet frames")
    ap.add_argument("--start", type=float, default=0, help="contact sheet start time")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--frame", type=float, help="extract one frame at this time")
    ap.add_argument("--out", default="frame.png", help="output file for --frame")
    args = ap.parse_args()

    duration, size, w, h = probe(args.video)
    print(f"{args.video}: {duration:.1f}s, {w}x{h}, {size / 1e6:.1f} MB")

    if args.scan:
        bad = scan(args.video, args.scan, duration)
        print("shrunken frames at:", bad if bad else "none")
    if args.frame is not None:
        with open(args.out, "wb") as f:
            f.write(frame_png(args.video, args.frame))
        print(f"wrote {args.out}")
    if args.sheet:
        n = max(1, math.ceil((duration - args.start) / args.every))
        rows = math.ceil(n / args.cols)
        if rows > 8:
            print(f"note: {rows} rows; consider --start/--every to make several smaller sheets", file=sys.stderr)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(args.start), "-i", args.video, "-vf",
                        f"fps=1/{args.every},scale=480:-1,tile={args.cols}x{rows}", "-frames:v", "1", args.sheet],
                       check=True)
        print(f"wrote {args.sheet} ({n} frames)")


if __name__ == "__main__":
    main()
