#!/usr/bin/env python3
"""Cut a raw demo recording into the final video.

segments.json is a list of cuts in raw-video seconds:
    [{"start": 11.0, "end": 25.3, "caption": "Chat with the co-pilot"},
     {"start": 85.4, "end": 286.2, "speed": 25, "caption": "Fitting, sped up 25x", "caption_whole": true}]
`speed` defaults to 1. A caption shows for the first 4 s unless caption_whole is true.
The first segment fades in and the last fades out.

    python edit_video.py segments.json raw_take.webm demo.mp4 [--webm] [--font FILE]
    python edit_video.py segments.json --map 305.5 736.5     # raw seconds -> output seconds

Each segment is rendered by its own ffmpeg process and the parts are joined with the concat
demuxer (no re-encode). One big filter graph that trims many segments from a single input
buffers frames for every branch and can exhaust RAM on long, high-resolution takes.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",            # Debian/Ubuntu
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",          # Fedora
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",               # macOS
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",                                    # Windows
    "C:/Windows/Fonts/segoeuib.ttf",
]


def find_font(explicit: str | None) -> str:
    if explicit:
        return explicit
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    if shutil.which("fc-match"):
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True)
        if out.stdout.strip():
            return out.stdout.strip()
    sys.exit("no caption font found; pass --font /path/to/font.ttf")


def filter_path(p) -> str:
    """Escape a path for use inside an ffmpeg filter argument (Windows drive colons etc.)."""
    return str(Path(p).as_posix()).replace(":", r"\:").replace("'", r"\'")


def output_time(segments, raw: float):
    t = 0.0
    for s in segments:
        speed = s.get("speed", 1)
        if s["start"] <= raw <= s["end"]:
            return t + (raw - s["start"]) / speed
        t += (s["end"] - s["start"]) / speed
    return None


def render(segments, src, out, font, fps, threads, workdir: Path):
    parts = []
    for i, seg in enumerate(segments):
        speed = seg.get("speed", 1)
        length = (seg["end"] - seg["start"]) / speed
        vf = [f"setpts=PTS/{speed}", f"fps={fps}"]
        if seg.get("caption"):
            txt = workdir / f"caption{i:02d}.txt"
            txt.write_text(seg["caption"], encoding="utf-8")
            enable = "" if seg.get("caption_whole") else ":enable='lt(t,4)'"
            # expansion=none: show '%' etc. literally instead of as drawtext expressions.
            vf.append(f"drawtext=fontfile='{filter_path(font)}':textfile='{filter_path(txt)}':expansion=none"
                      f":fontsize=24:fontcolor=white:box=1:boxcolor=0x0b0b0b@0.72:boxborderw=16"
                      f":x=(w-text_w)/2:y=h-text_h-56{enable}")
        if i == 0:
            vf.append("fade=t=in:st=0:d=0.6")
        if i == len(segments) - 1:
            vf.append(f"fade=t=out:st={max(length - 0.8, 0):.2f}:d=0.8")
        part = workdir / f"part{i:02d}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", str(threads),
                        "-ss", str(seg["start"]), "-to", str(seg["end"]), "-i", str(src),
                        "-vf", ",".join(vf), "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                        "-pix_fmt", "yuv420p", "-threads", str(threads), str(part)], check=True)
        parts.append(part)
        print(f"part {i:02d}: {seg['start']}-{seg['end']}s x{speed} -> {length:.1f}s", flush=True)

    listing = workdir / "parts.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(out)], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("segments", type=Path)
    ap.add_argument("src", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--webm", action="store_true", help="also write a VP9 .webm next to the .mp4")
    ap.add_argument("--font", help="TTF/OTF font file for captions")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--threads", type=int, default=4, help="ffmpeg threads per process")
    ap.add_argument("--map", nargs="+", type=float, metavar="RAW_S", help="print output time for raw times")
    args = ap.parse_args()

    segments = json.loads(args.segments.read_text(encoding="utf-8"))
    if args.map:
        for raw in args.map:
            t = output_time(segments, raw)
            print(f"raw {raw}s -> " + (f"output {t:.1f}s" if t is not None else "cut out"))
        return
    if not (args.src and args.out):
        ap.error("src and out are required unless --map is used")

    font = find_font(args.font)
    with tempfile.TemporaryDirectory(prefix="demo_edit_") as tmp:
        render(segments, args.src, args.out, font, args.fps, args.threads, Path(tmp))
    total = sum((s["end"] - s["start"]) / s.get("speed", 1) for s in segments)
    print(f"wrote {args.out} (~{total:.0f}s)")

    if args.webm:
        webm = str(Path(args.out).with_suffix(".webm"))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", args.out, "-c:v", "libvpx-vp9", "-crf", "32",
                        "-b:v", "0", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
                        "-threads", str(args.threads), webm], check=True)
        print(f"wrote {webm}")


if __name__ == "__main__":
    main()
