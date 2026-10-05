# Troubleshooting

Each entry describes a real failure from recording app demos: the symptom, the cause, and the fix.

## Setup

**"Executable doesn't exist at …/chromium-NNNN" on first navigate**
The MCP's bundled Playwright expects a different Chromium build than the one installed.
Install the matching build through the MCP package:
`npx -y --package=@playwright/mcp@<version> -- playwright-core install chromium`.

**Headed Chromium exits immediately with code 127 (Linux/WSL)**
A shared library is missing, usually `libasound.so.2`. Run
`ldd ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome | grep "not found"` to see which
one. Have the user install it (`sudo apt install libasound2t64`), or install everything with
`playwright-core install-deps chromium`.

**Emoji show as empty boxes in the video**
No color-emoji font is installed. Install `fonts-noto-color-emoji` (Linux). macOS and Windows
ship emoji fonts.

**MCP tools don't appear after `claude mcp add`**
MCP servers only load at session start. The user must restart Claude Code or reload the IDE
window, then approve the project server.

## Recording

**The page fills only part of the frame, with grey bars on the right and bottom**
The browser window was moved, resized, minimized or dragged to another monitor during the
take, so Chromium drew a smaller page inside the fixed-size recording. Ask the user to
leave the window alone. Check every raw take with `check_video.py --scan 20`. Reloading the
page or adding init scripts does *not* cause this.

**A chart is blank in the video even though the data was there**
A chart sized to its container (Vega-Lite `width: "container"`, CSS-percentage canvases) was
drawn while its tab or panel was hidden, so it measured 0 px wide and never re-measured.
The usual app fix is to fire `window.dispatchEvent(new Event('resize'))` whenever a tab is
shown, or to render charts lazily when they become visible. Avoid the trap in recordings
by being on the relevant tab when results arrive, and check each chart frame in the final cut.

**`browser_run_code_unsafe` fails or the file chooser stays open**
Native file pickers are intercepted by the MCP. Click the upload control in one step, call
`browser_file_upload` with an absolute path, then continue in the next step.

**Variables from a previous step are undefined**
Each `browser_run_code_unsafe` call runs in a fresh scope, and `globalThis` doesn't persist.
Bake constants (T0) into each step file with `make_step.py`. Re-time all step files for a
new take with `make_step.py --retime <T0> <files...>`.

**A tool call hangs or times out during a long job**
Keep each call under about 2 minutes. Poll with `waitIdle(...)` and call the wait step again
until it reports done.

**A background job is still running after an aborted take**
Closing the browser doesn't stop server-side work. Check the app's logs and GPU/CPU use, and
restart the app server if needed before the next take. Otherwise the next take competes
with it.

**The app behaves differently in the take than in testing (e.g. an AI agent skips a confirmation)**
Agent behaviour varies from run to run. Make the instruction explicit in the app's prompt or
tool docstring rather than hoping it repeats. Rehearse that step once before the real take.

**Timestamps don't line up with the video**
T0 came from the file's last-write time instead of its creation time (Linux, script run too
late). Find a sharp visual event (a new row appearing, a tab switch), locate its frame with
`check_video.py --frame`, and shift T0 by the difference. In practice, running
`video_start_time.py` right after `browser_navigate` gives an offset under 1 s.

## Editing

**The machine runs out of memory or WSL crashes during the edit**
That happens with a single ffmpeg graph that splits one input into many trimmed branches.
Use `edit_video.py`, which renders one segment per process and peaks at about 400 MB for a
1440×900 take.

**Captions show garbage or warn "Stray %"**
drawtext treats `%` as the start of an expression. `edit_video.py` sets `expansion=none` and
passes captions through text files, so any characters are safe.

**"No such filter: drawtext"**
The ffmpeg build lacks freetype. Install a full build (Homebrew `ffmpeg`, Gyan builds on
Windows, distro packages on Linux).

**Caption font not found**
Pass `--font` with a TTF/OTF path. On Windows, use forward slashes (`C:/Windows/Fonts/arialbd.ttf`).

**Something wrong or misleading is visible (e.g. a mislabelled table)**
Cut it with the segment list rather than shipping it. Then fix the cause in the app and
offer to re-record.
