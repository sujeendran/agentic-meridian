#!/usr/bin/env python3
"""Print when the newest recording in a folder started, as epoch milliseconds (T0).

Run it right after `browser_navigate` opens the page. Playwright creates the video file
when recording starts, so its creation time is the zero point of the raw video.

    python video_start_time.py demo_videos
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def start_time_ms(path: Path) -> tuple[int, str]:
    st = path.stat()
    birth = getattr(st, "st_birthtime", None)  # macOS, BSD, Windows (Python 3.12+)
    if birth:
        return int(birth * 1000), "creation time"
    # Linux exposes no birth time via os.stat. Until Playwright flushes the first frames
    # the file is empty and its mtime is still its creation time.
    if st.st_size == 0:
        return int(st.st_mtime * 1000), "creation time (empty file mtime)"
    return int(st.st_mtime * 1000), "LAST-WRITE time (file already has data: run this sooner, or verify the offset)"


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_videos")
    # Playwright names live recordings page@<id>.webm; ignore edited outputs in the same folder.
    videos = sorted(list(folder.glob("page@*.webm")) or list(folder.glob("*.webm")), key=os.path.getmtime)
    if not videos:
        sys.exit(f"no .webm recordings in {folder}")
    newest = videos[-1]
    ms, source = start_time_ms(newest)
    print(ms)
    print(f"{newest.name}: {source}", file=sys.stderr)


if __name__ == "__main__":
    main()
