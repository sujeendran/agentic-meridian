---
name: demo-video-recording
description: Record and edit a polished demo video of a web application by driving a real browser with the Playwright MCP server, then cutting the recording with ffmpeg (trim idle gaps, speed up long waits, add captions, fades). Use this skill whenever the user wants a demo video, product walkthrough, screen recording, screencast, feature tour or "video of the app", even if they don't mention Playwright or ffmpeg. Also use it when they ask to re-record or re-edit an existing demo, add a feature to one, or fix something visible in one. Covers dependency setup on macOS, Windows and Linux/WSL, storyboarding, seeding app state off-camera, recording long-running flows, and memory-safe editing.
---

# Demo video recording

You are going to make a short, polished video of a web app. A visible browser is driven
through the Playwright MCP server, which records it, and ffmpeg turns the raw take into
the final cut. The user watches the browser live if they want, but they should never
have to babysit it.

The work has five phases. Each one exists because skipping it cost real time before:

1. **Set up**: dependencies, MCP server, recording config ([Setup](#1-setup)).
2. **Storyboard**: agree what the video shows and what happens off-camera ([Storyboard](#2-storyboard)).
3. **Record**: drive the app with step scripts while logging timestamps ([Record](#3-record)).
4. **Edit**: cut segments with `scripts/edit_video.py` ([Edit](#4-edit)).
5. **Verify**: check frames before calling it done ([Verify](#5-verify)).

Bundled files (paths relative to this skill's directory):

| File | Use |
|---|---|
| `assets/playwright-mcp-config.json` | MCP config: headed Chromium, fixed viewport, video recording |
| `assets/cursor-overlay.js` | Visible cursor and click ripple (Playwright videos show no pointer) |
| `assets/step-helpers.js` | Helper functions included in every step script |
| `scripts/make_step.py` | Builds a runnable step script from helpers, the recording's start time and a step body |
| `scripts/video_start_time.py` | Prints the start time of the newest recording (epoch ms) |
| `scripts/edit_video.py` | Cuts the raw take from a segments JSON file: speed-ups, captions, fades |
| `scripts/check_video.py` | Finds shrunken or grey-padded frames and builds contact sheets |
| `references/troubleshooting.md` | Every failure seen so far and its fix. Read it when something looks wrong |

## 1. Setup

Check what's already installed before installing anything, and ask the user to run
commands that need admin rights or `sudo`. Don't attempt those yourself.

**Required everywhere**
- **Node.js 18+**, for `npx` and the MCP server.
- **ffmpeg** built with `libx264`, `libvpx` and `drawtext` (freetype). Most full builds have all
  three. Check with `ffmpeg -hide_banner -filters | grep drawtext` (Windows: `findstr`).
  - macOS: `brew install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg` (or `choco install ffmpeg`)
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - Fedora: `sudo dnf install ffmpeg`
- **Python 3.9+** with Pillow (`pip install pillow`), for `scripts/check_video.py`. The other
  scripts use only the standard library.

**Browser.** Each Playwright MCP version expects one specific Chromium build. Install
exactly that build through the MCP's own copy of Playwright (pin the same version you
register):
```
npx -y --package=@playwright/mcp@<version> -- playwright-core install chromium
```
If you skip this, the first navigation fails with "Executable doesn't exist at …/chromium-NNNN".
As an alternative, set `"browserName": "chromium"` plus `"channel": "chrome"` in the
config to use an installed Google Chrome.

**Linux and WSL extras**
- Headed Chromium needs system libraries. Install them with
  `sudo npx -y --package=@playwright/mcp@<version> -- playwright-core install-deps chromium`.
  If `libasound2` is the only one missing, `sudo apt install libasound2t64` is enough.
- Install an emoji font (`sudo apt install fonts-noto-color-emoji`). Without it, any ✅ ⚠️ 🔴 the
  app or an AI agent prints shows up as empty boxes in the video.
- On WSL2, headed windows appear on the Windows desktop through WSLg. Check that `DISPLAY` is set.

**Register the MCP server** for the project, pinned to a version:
```
codex mcp add --scope project playwright -- npx -y @playwright/mcp@<version> --config .playwright-mcp/config.json
```
- Copy `assets/playwright-mcp-config.json` to `.playwright-mcp/config.json` and adjust the
  viewport. 1440×900 reads well; keep `recordVideo.size` equal to the viewport.
- Add `.playwright-mcp/output/` and the video folder (`demo_videos/` by default) to `.gitignore`.
- **The user must restart Codex (or reload the IDE window) and approve the server.** MCP
  tools only load at session start. Say so plainly and wait for them.

With this config, every browser session is recorded. A video file appears when the page
opens and is finished when it closes (`browser_close`).

## 2. Storyboard

Agree on the storyboard with the user before recording: a numbered list of what's shown,
the exact prompts or inputs, and the target length. Long takes are expensive to redo,
and a misread requirement costs a whole take.

- **Seed state off-camera.** If a feature only looks good from a particular starting state,
  create that state before the visible part starts and trim it out. Examples: worse starting
  parameters so an optimizer visibly improves things, a pre-loaded dataset, a logged-in user.
  Use the app's own API from the page (`page.evaluate(() => fetch(...))`). Don't type it into
  the UI or a chat box. Users notice if the demo "cheats" on screen, and asked-for hidden setup
  should stay hidden.
- **One continuous session.** Avoid splicing separate sessions; state, chat history and IDs won't
  line up. If two features are slow (e.g. two long jobs), combine them in one flow rather than
  running each separately.
- **Keep on-screen text short.** Ask AI agents for "briefly" or "in two sentences" answers; long
  replies make dead air.
- **Plan for slow steps.** Anything that takes minutes gets shown starting, sped up (×4–×25) with
  a caption saying so, and shown finishing.
- **Tell the user the expected recording time** (raw take, not final length) and that they don't
  need to watch. Ask them not to move, resize or minimize the browser window during the take.
  If they do, the recording shrinks and gets grey bars.

Do one quick, unrecorded rehearsal of the risky parts if the flow is new: uploads, long jobs,
anything involving an AI agent. That's where surprises show up (wrong behaviour, wrapped
labels, missing fonts). Fix them in the app or the storyboard, then record the real take.

## 3. Record

### Start the take and anchor time

1. Make sure the app is running and the GPU or other worker resources are idle.
2. Call `browser_navigate` to the app URL. This opens the page and starts the video file.
3. Straight away, run `python scripts/video_start_time.py <video-dir>`. It prints the newest
   recording's start time as epoch milliseconds: **T0**. Every timestamp you log is
   `(Date.now() - T0) / 1000`, which is a position in the raw video. The editor needs these
   positions.

### Step scripts

Run each storyboard step with `browser_run_code_unsafe` using its `filename` parameter. Don't
paste large inline code; it bloats the conversation and invites copy-paste drift. Build the
files with `make_step.py`:
```
python scripts/make_step.py --t0 <T0> --out .playwright-mcp/output/demo/s1_intro.js <<'EOF'
  mark('s1_start');
  await say('Briefly summarize the dataset.');
  await settle();
  mark('s1_end');
  return { marks };
EOF
```
(On Windows PowerShell, write the body to a file and pass `--body file.js`.) To re-record with
the same steps later, retime them: `python scripts/make_step.py --retime <new T0> .playwright-mcp/output/demo/*.js`.

- Step files must live **inside the workspace**, e.g. `.playwright-mcp/output/demo/`. The MCP only
  reads files under the workspace roots.
- `make_step.py` prepends `assets/step-helpers.js`, which gives you:
  - `mark(name)`: log a timestamp
  - `moveTo` / `click`: move the visible cursor smoothly to an element, then click
  - `say(text)`: type into a text input and press Enter. Its selector is a placeholder; adapt it
  - `waitIdle(...)`: wait for a condition with a timeout, without throwing
  - `scrollTo(...)`: smooth-scroll a container
- Edit the helpers file to fit the app's selectors.
- **Install the cursor overlay first** with `page.addInitScript({ path: '<skill>/assets/cursor-overlay.js' })`,
  followed by `page.reload()`. The reload creates a new app session in many apps, so seed
  off-camera state *after* it.
- Return the marks from every step, plus a few checks of on-screen state such as current
  values or the latest message. That's how you notice problems while the take is still
  recoverable.

### Long waits

- Each MCP tool call should finish in about 2 minutes or less. For a job that takes 5 minutes,
  run a `wait` step that polls for completion with `waitIdle(..., 115000)` and returns
  whether it finished. Call it repeatedly until it reports done.
- Variables don't carry over between `browser_run_code_unsafe` calls (`globalThis` is reset).
  Anything a later step needs, such as T0, must be baked into the file.
- Tell the user, in a few words, how the take is going at each milestone (e.g. "round 2 done,
  gap 6.4% → 3.1%").

### File uploads

The MCP intercepts native file pickers. Click the upload control in a step, then call
`browser_file_upload` with an absolute path, then run the next step. Doing the whole thing
inside one script with `page.waitForEvent('filechooser')` fails.

### Finish

Call `browser_close` to finalize the video, then rename the newest `page@*.webm` to something
meaningful (`raw_take.webm`). Stop any servers or background jobs you started.

## 4. Edit

Write a segments file: a JSON list of cuts in **raw-video seconds**, taken from your marks.
```json
[
  {"start": 11.0, "end": 25.3, "caption": "Chat with your co-pilot"},
  {"start": 85.4, "end": 286.2, "speed": 25, "caption": "Fitting (~4 min), sped up 25x", "caption_whole": true},
  {"start": 286.2, "end": 293.5, "caption": "Round 1: gap 6.2%, target 3%", "caption_whole": true},
  {"start": 293.8, "end": 314.2}
]
```
Each segment can set:
- `speed`: optional, default 1.
- `caption`: optional. It shows for the first 4 seconds unless `caption_whole` is true.

The first segment fades in and the last fades out.

```
python scripts/edit_video.py segments.json raw_take.webm demo.mp4 --webm
```

How to cut:
- **Leave out the dead time between steps.** That's the gaps between your marks: agent thinking
  time, tool-call latency, setup.
- **Start just before the first visible action** and end on a strong final frame.
- **Cut anything wrong or misleading** instead of shipping it, e.g. an AI reply that mislabels
  data. Tell the user what you cut and why, and offer to fix the cause and re-record.
- **Put real numbers in captions** ("gap 6.4% → 3.1%"). A silent video relies on them.

`edit_video.py` renders every segment in its **own ffmpeg process** and joins the parts
without re-encoding. That design is deliberate. A single filter graph that trims many
segments from one input buffers decoded frames for every branch, and at 1440×900 this
used up 16 GB of RAM and crashed the machine (WSL) once.

## 5. Verify

Before telling the user it's done:

```
python scripts/check_video.py raw_take.webm --scan 20            # shrunken / grey-padded frames
python scripts/check_video.py demo.mp4 --sheet sheet.png --every 8 # contact sheet of the final cut
python scripts/check_video.py demo.mp4 --frame 100.3 --out f.png   # one specific moment
```

- **Look at the contact sheet and at key frames** with the image viewer (Read tool). Check that
  captions render, each section is present, and the ending is clean.
- **Check every chart or visual at the moment it's on screen.** Things that look fine in a live
  check can be blank in the recording. A container-width chart drawn while its tab was hidden
  rendered at 0 px. To find the right output time, map raw seconds through the segment list
  (`edit_video.py --map <raw-seconds>` prints it).
- **Report** the final file path, length and size, what's shown section by section with the
  key numbers, anything you cut or worked around, and what's left in the video folder.

When something misbehaves, read `references/troubleshooting.md`.
