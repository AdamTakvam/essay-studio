# Essay Studio

Write an essay with a live Claude co-writer, turn it into a summary, an audiobook, a two-host podcast and a video, review and approve the results, then package everything for social media.

## Run it

```bash
cp .env.example .env        # add your ANTHROPIC_API_KEY
./run.sh                    # creates .venv on first run
# open http://localhost:8765
```

You need Python 3.10 or newer, `ffmpeg` on PATH, and internet access for Claude and Microsoft Edge's neural voices. The API key lives on the server, so the UI has no settings page.

## Workflow

| Stage | What happens |
|---|---|
| **1 Write** | Type in the left pane. Claude rewrites it live in the right pane, using the model and style prompt above that pane. **←** copies the AI draft over your essay; the first time, a dialog asks you to confirm, and Ctrl+Z undoes it. **Save** writes to disk (Ctrl+S also works). **Create media** saves and moves to the next stage. |
| **2 Media** | Tick formats, adjust settings, generate. Each card shows a progress bar and a status, then a player or text so you can review it. Then you **Approve** it. If you edit the essay after generating, the card is marked *Outdated*. |
| **3 Publish** | Shows the essay plus only the approved media. Tick the platforms and press **PUBLISH**. You get a zip per platform. |

Click any step in the header, or use the back buttons, to return to the essay, make a change and run it again. Speech that's already rendered is cached, so a re-run only re-renders what changed.

The bottom pane streams every tool call: Claude requests and token counts, edge-tts per-turn progress with ETA, ffmpeg and the publisher. Drag the divider to resize it, or double-click it to reset to 85/15.

## How each format is made

- **Summary:** Claude writes it in one of three styles: an abstract, key points, or a TL;DR.
- **Audiobook:** the essay text, with markdown removed, is split into paragraph chunks and spoken by one edge-tts voice at your chosen speed.
- **Podcast:** Claude writes an `ANDREW:` / `EMMA:` style script. The hosts explain the essay, challenge claims that aren't well supported using outside knowledge, and respond sincerely to its valid emotional points. The script is rendered with the **render-deepdive.ps1 technique** (see `server/tts.py`):
  - one edge-tts request per turn, up to 5 attempts with a 3, 6, 12, 24 s backoff
  - each turn converted to 24 kHz mono WAV, with 0.25 s of silence between turns
  - ffmpeg concat into AAC 128k `.m4a`
  - a SHA-256 build cache, so an interrupted render resumes
  - an ETA based only on turns rendered fresh in the current run
- **Video:** Claude first classifies the essay as *expository* or *narrative*.
  - Expository essays become an explainer: titled slides with bullets and a host narrating each one.
  - Narrative essays become a story: scene cards with a narrator retelling each scene.
  - Slides are drawn with Pillow, narration comes from edge-tts, and ffmpeg encodes a 1080p MP4.

## Publish packages

`data/projects/<id>/publish/<run>/<platform>/` and a zip of it contain:

- `essay.md` (always), `summary.md`
- `post.json` / `post.txt`: platform copy that respects the length limits (YouTube title and description, TikTok and Instagram captions, an X thread, LinkedIn and Facebook posts)
- `video.mp4`: 16:9 for YouTube, Facebook, X and LinkedIn. A 9:16 version with blurred fill for TikTok and Instagram Reels. If you approved audio but no video, YouTube and Facebook get the audio over a title card.
- an Instagram slide carousel, `thumbnail.png`, the audio file where it's useful
- `manifest.json`, with warnings, for example a video longer than a platform's limit

**Direct posting is not connected yet.** Each platform needs its own developer app and OAuth. `server/publish.py` is the place to add uploaders; each package's `manifest.json` already lists what an uploader would send.

## Layout

```
server/app.py      HTTP API + Server-Sent Events log stream
server/llm.py      Anthropic client (model list from the Models API)
server/tts.py      edge-tts renderer (port of render-deepdive.ps1)
server/media.py    summary / audiobook / podcast / video jobs
server/slides.py   slide & title-card images
server/publish.py  per-platform packaging
web/               single-page UI (no build step)
data/              projects, media, packages, TTS cache (git-ignored)
```

To test offline, run `ESSAY_STUDIO_FAKE_LLM=1 ESSAY_STUDIO_FAKE_TTS=1 ./run.sh`. This replaces Claude and edge-tts with offline stand-ins.
