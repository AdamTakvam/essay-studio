"""Media generation jobs: summary, audiobook, two-host podcast, explainer/story video."""
import asyncio
import json
import re
import shutil
import time
import traceback
from pathlib import Path

from . import llm, slides, store, tts
from .logbus import bus, logger

log = logger("pipeline")
_tasks: dict[tuple[str, str], asyncio.Task] = {}

PODCAST_WORDS = {"short": 900, "standard": 1800, "long": 3200}


# ---------------------------------------------------------------- job plumbing
class Job:
    def __init__(self, pid: str, kind: str, options: dict):
        self.pid, self.kind, self.options = pid, kind, options
        self.dir = store.project_dir(pid) / "media" / kind
        self._last_save = 0.0
        self.state: dict = {}

    async def set(self, persist: bool = True, **fields) -> None:
        self.state.update(fields)
        bus.emit("job", project=self.pid, media=self.kind, state=dict(self.state))
        now = time.time()
        if persist or now - self._last_save > 1.0:
            self._last_save = now
            st = dict(self.state)
            await store.update(self.pid, lambda d: d["media"].__setitem__(self.kind, st))

    def progress(self, frac: float, message: str) -> None:
        async def _apply():
            if self.state.get("status") == "running":  # never overwrite a finished state
                await self.set(persist=False, progress=round(max(0.0, min(1.0, frac)), 3), message=message)
        asyncio.get_running_loop().create_task(_apply())

    def rel(self, p: Path) -> str:
        return p.relative_to(store.project_dir(self.pid)).as_posix()


def is_running(pid: str, kind: str) -> bool:
    t = _tasks.get((pid, kind))
    return bool(t and not t.done())


async def start(pid: str, kind: str, options: dict) -> dict:
    if kind not in store.MEDIA_KINDS:
        raise ValueError(f"Unknown media type {kind}")
    if is_running(pid, kind):
        raise RuntimeError(f"{kind} is already being generated")
    project = store.load(pid)
    if not project["essay"].strip():
        raise ValueError("The essay is empty — write something first")
    job = Job(pid, kind, options)
    job.state = {"status": "queued", "progress": 0, "message": "Queued", "files": {}, "approved": False,
                 "essay_hash": project["essay_hash"], "options": options, "error": None,
                 "started": time.time(), "finished": None, "meta": {}}
    await job.set()
    _tasks[(pid, kind)] = asyncio.create_task(_run(job, project))
    return job.state


async def cancel(pid: str, kind: str) -> bool:
    t = _tasks.get((pid, kind))
    if t and not t.done():
        t.cancel()
        return True
    return False


async def _run(job: Job, project: dict) -> None:
    runner = {"summary": _summary, "audiobook": _audiobook, "podcast": _podcast, "video": _video}[job.kind]
    log(f"▶ {job.kind}: started for “{project['title']}”")
    try:
        if job.dir.exists():
            shutil.rmtree(job.dir)
        job.dir.mkdir(parents=True, exist_ok=True)
        await job.set(status="running", message="Starting…")
        await runner(job, project)
        await job.set(status="done", progress=1, message="Ready for review", finished=time.time())
        log(f"✔ {job.kind}: finished in {time.time() - job.state['started']:.0f}s")
    except asyncio.CancelledError:
        await job.set(status="cancelled", message="Cancelled", finished=time.time())
        log(f"■ {job.kind}: cancelled", "warn")
    except Exception as e:
        log(f"✖ {job.kind}: {type(e).__name__}: {e}", "error")
        log(traceback.format_exc(limit=4), "error")
        await job.set(status="error", message=str(e), error=str(e), finished=time.time())


def _model(job: Job, project: dict) -> str:
    return job.options.get("model") or project.get("model") or "claude-sonnet-4-5"


def plain_text(md: str) -> str:
    """Strip markdown syntax so TTS doesn't read symbols aloud."""
    t = re.sub(r"```.*?```", "", md, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"[*_`~]{1,3}", "", t)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    return t.strip()


def _paragraph_chunks(text: str, limit: int = 2500) -> list[str]:
    chunks, cur = [], ""
    for para in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
        if cur and len(cur) + len(para) + 2 > limit:
            chunks.append(cur)
            cur = ""
        if len(para) > limit:  # very long paragraph: split on sentences
            for sent in re.split(r"(?<=[.!?])\s+", para):
                if cur and len(cur) + len(sent) + 1 > limit:
                    chunks.append(cur)
                    cur = ""
                cur = f"{cur} {sent}".strip()
        else:
            cur = f"{cur}\n\n{para}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _speaker_tag(voice_id: str) -> str:
    name = voice_id.split("-", 2)[-1].replace("Neural", "").replace("Multilingual", "")
    return re.sub(r"[^A-Z]", "", name.upper()) or "HOST"


# ---------------------------------------------------------------- summary
SUMMARY_STYLES = {
    "abstract": "an academic-style abstract: one tight paragraph stating the thesis, the main supporting "
                "arguments, and the conclusion",
    "key-points": "a one-sentence summary followed by 4–7 bullet points of the key ideas (markdown bullets)",
    "tldr": "a 1–2 sentence TL;DR suitable for a social media post",
}


async def _summary(job: Job, project: dict) -> None:
    style = job.options.get("style", "abstract")
    words = int(job.options.get("words", 180))
    job.progress(0.1, "Asking Claude for a summary…")
    system = ("You summarize essays faithfully. Never invent claims the author did not make. "
              "Output only the summary text, no preamble.")
    prompt = (f"Write {SUMMARY_STYLES.get(style, SUMMARY_STYLES['abstract'])}, about {words} words or fewer, "
              f"for the essay below.\n\n<essay>\n{project['essay']}\n</essay>")
    text = await llm.complete(_model(job, project), system, prompt, label="summary", max_tokens=2000,
                              fake=lambda: _fake_summary(project["essay"], style))
    out = job.dir / "summary.md"
    out.write_text(text, encoding="utf-8")
    await job.set(files={"text": job.rel(out)}, meta={"words": len(text.split()), "style": style})


# ---------------------------------------------------------------- audiobook
async def _audiobook(job: Job, project: dict) -> None:
    voice = job.options.get("voice", "en-US-AndrewNeural")
    rate = tts.rate_str(job.options.get("rate", 0))
    chunks = _paragraph_chunks(plain_text(project["essay"]))
    turns = [tts.Turn("NARRATOR", c) for c in chunks]
    out = job.dir / "audiobook.m4a"
    res = await tts.render(turns, {"NARRATOR": {"voice": voice, "rate": rate}}, out,
                           label=f"audiobook-{project['id']}", transition=0.45, progress=job.progress,
                           fresh=bool(job.options.get("fresh")))
    await job.set(files={"audio": job.rel(out)},
                  meta={"duration": round(sum(res.durations) + 0.45 * (len(turns) - 1), 1),
                        "voice": voice, "rate": rate, "segments": len(turns)})


# ---------------------------------------------------------------- podcast
async def _podcast(job: Job, project: dict) -> None:
    v1 = job.options.get("voice1", "en-US-AndrewNeural")
    v2 = job.options.get("voice2", "en-US-EmmaNeural")
    a, b = _speaker_tag(v1), _speaker_tag(v2)
    if a == b:
        a, b = a + "A", b + "B"
    target = PODCAST_WORDS.get(job.options.get("length", "standard"), 1800)
    na, nb = a.title(), b.title()

    job.progress(0.03, "Claude is writing the podcast script…")
    system = f"""You write scripts for a two-host audio show: a lively, intelligent "deep dive" conversation about one piece of writing.
The hosts are {na} and {nb}. They are curious, warm, sharp, and allowed to disagree with the text and with each other.

OUTPUT FORMAT (strict — the script is fed straight into a text-to-speech engine):
- Every turn starts on its own line with the host name in capitals and a colon: "{a}:" or "{b}:", followed by the spoken words.
- Only words meant to be spoken aloud. No headings, markdown, stage directions, sound cues, brackets, or emojis.
- Write numbers, symbols and abbreviations the way they should be pronounced.

CONTENT:
- Open with a hook, then explain the essay's central argument clearly and faithfully, citing its ideas specifically.
- Where the essay makes claims that are not well supported, challenge them: bring in relevant outside knowledge, counter-examples, research, or history, and say what evidence would settle it.
- Where the essay makes a valid emotional point, slow down and relate to it sincerely — share how it lands, connect it to ordinary experience.
- Keep it conversational: short reactions, follow-up questions, the occasional interruption — but every turn should add something.
- Close with a clear takeaway and a question for the listener.
- Length: about {target} words in total."""
    prompt = f"Here is the essay the hosts are discussing.\n\n<essay>\n{project['essay']}\n</essay>\n\nWrite the full script now."
    script = await llm.complete(_model(job, project), system, prompt, label="podcast script",
                                max_tokens=max(4000, int(target * 2.2)),
                                fake=lambda: _fake_podcast(project["essay"], a, b))
    script = re.sub(rf"^\**({a}|{b})\**\s*:", r"\1:", script, flags=re.M)  # tolerate **NAME:**
    script_file = job.dir / "script.txt"
    script_file.write_text(script, encoding="utf-8")
    turns = tts.parse_transcript(script, [a, b])
    if len(turns) < 2:
        raise RuntimeError(f"Script did not contain {a}:/{b}: turns — see script.txt")
    log(f"podcast: script has {len(turns)} turns, {len(script.split()):,} words")
    await job.set(files={"script": job.rel(script_file)}, meta={"turns": len(turns)})

    out = job.dir / "podcast.m4a"
    voices = {a: {"voice": v1, "rate": tts.rate_str(job.options.get("rate1", 5))},
              b: {"voice": v2, "rate": tts.rate_str(job.options.get("rate2", 15))}}

    def prog(f, m):
        job.progress(0.15 + 0.85 * f, m)
    res = await tts.render(turns, voices, out, label=f"podcast-{project['id']}", progress=prog)
    await job.set(files={"audio": job.rel(out), "script": job.rel(script_file)},
                  meta={"turns": len(turns), "hosts": [na, nb],
                        "duration": round(sum(res.durations) + 0.25 * (len(turns) - 1), 1)})


# ---------------------------------------------------------------- video
async def _video(job: Job, project: dict) -> None:
    model = _model(job, project)
    voice = job.options.get("voice", "en-US-AvaNeural")
    rate = tts.rate_str(job.options.get("rate", 5))

    job.progress(0.03, "Claude is assessing the essay (expository or story?)…")
    verdict = llm.parse_json(await llm.complete(
        model,
        "You classify writing. Reply with JSON only.",
        "Is the following text primarily EXPOSITORY (explains, argues, informs) or NARRATIVE (tells a story with "
        "characters and events)? Reply as {\"kind\": \"expository\" | \"narrative\", \"reason\": \"one sentence\"}."
        f"\n\n<essay>\n{project['essay']}\n</essay>",
        label="video: classify", max_tokens=300,
        fake=lambda: json.dumps({"kind": "expository", "reason": "It argues a point (fake mode)."})))
    kind = "narrative" if str(verdict.get("kind", "")).lower().startswith("narr") else "expository"
    log(f"video: content is {kind.upper()} — {verdict.get('reason', '')}")
    await job.set(meta={"kind": kind, "reason": verdict.get("reason", "")})

    job.progress(0.08, f"Claude is storyboarding the {'explainer' if kind == 'expository' else 'story'} video…")
    if kind == "expository":
        spec = ('{"title": str, "subtitle": str, "intro": str, "slides": [{"heading": str, "bullets": [str], '
                '"narration": str}]}')
        rules = ("Make an explainer video: a slideshow with one host narrating. 5–9 slides that walk through the "
                 "essay's points in order. Each slide: a short heading (≤ 8 words), 2–4 bullets (≤ 12 words each), "
                 "and narration of 50–110 words in which the host summarizes and explains that point in a warm, "
                 "clear voice. 'intro' (30–60 words) is spoken over the title slide. The last slide wraps up.")
    else:
        spec = ('{"title": str, "subtitle": str, "intro": str, "slides": [{"heading": str, "caption": str, '
                '"narration": str}]}')
        rules = ("Make a narrated story video (an enactment told by one narrator over still scene cards). 5–10 "
                 "scenes in story order. Each scene: a short title (≤ 6 words), a caption (one vivid line or quote "
                 "from the scene, ≤ 20 words), and narration of 50–120 words that retells the scene dramatically, "
                 "voicing key lines of dialogue. 'intro' (20–50 words) sets the scene over the title card.")
    plan = llm.parse_json(await llm.complete(
        model,
        "You are a video producer. Reply with JSON only, no commentary. Narration is read by a text-to-speech "
        "voice, so write it as natural spoken English with no markdown or symbols.",
        f"{rules}\n\nReturn JSON matching: {spec}\n\n<essay>\n{project['essay']}\n</essay>",
        label="video: storyboard", max_tokens=6000,
        fake=lambda: json.dumps(_fake_storyboard(project))))
    items = [s for s in plan.get("slides", []) if s.get("narration")]
    if not items:
        raise RuntimeError("Storyboard had no slides")
    (job.dir / "storyboard.json").write_text(json.dumps({"kind": kind, **plan}, indent=2), encoding="utf-8")

    job.progress(0.15, f"Rendering {len(items) + 1} slide images…")
    title = plan.get("title") or project["title"]
    imgs = [job.dir / "slides" / "00_title.png"]
    await asyncio.to_thread(slides.render_slide, imgs[0], kind=kind, heading=title,
                            caption=plan.get("subtitle", ""), is_title=True)
    for i, s in enumerate(items, 1):
        p = job.dir / "slides" / f"{i:02d}.png"
        await asyncio.to_thread(slides.render_slide, p, kind=kind, heading=s.get("heading", ""),
                                bullets=s.get("bullets"), caption=s.get("caption", ""), index=i,
                                total=len(items), deck_title=title)
        imgs.append(p)
    log(f"video: rendered {len(imgs)} slide images")

    turns = [tts.Turn("HOST", plan.get("intro") or title)] + [tts.Turn("HOST", s["narration"]) for s in items]

    def tprog(f, m):
        job.progress(0.2 + 0.55 * f, m)
    res = await tts.render(turns, {"HOST": {"voice": voice, "rate": rate}}, None,
                           label=f"video-{project['id']}", transition=0, progress=tprog)

    segs = []
    for i, (img, wav) in enumerate(zip(imgs, res.segments)):
        job.progress(0.76 + 0.2 * i / len(imgs), f"Encoding segment {i + 1}/{len(imgs)}…")
        seg = job.dir / "segments" / f"seg_{i:02d}.mp4"
        seg.parent.mkdir(exist_ok=True)
        await tts.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30", "-i", str(img),
                       "-i", str(wav), "-af", "apad=pad_dur=0.7", "-c:v", "libx264", "-preset", "veryfast",
                       "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
                       "-ar", "48000", "-ac", "2", "-shortest", str(seg)])
        segs.append(seg)
    tts.log_ff(f"video: encoded {len(segs)} segments, joining…")
    concat = job.dir / "segments" / "concat.txt"
    concat.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
    out = job.dir / "video.mp4"
    await tts.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                   "-c", "copy", "-movflags", "+faststart", str(out)])
    shutil.rmtree(job.dir / "segments", ignore_errors=True)
    thumb = job.dir / "thumbnail.png"
    shutil.copy(imgs[0], thumb)
    duration = sum(res.durations) + 0.7 * len(segs)
    tts.log_ff(f"Wrote video.mp4 · {len(segs)} slides · {duration:.0f}s")
    await job.set(files={"video": job.rel(out), "thumbnail": job.rel(thumb),
                         "storyboard": job.rel(job.dir / "storyboard.json")},
                  meta={"kind": kind, "reason": verdict.get("reason", ""), "slides": len(items),
                        "duration": round(duration, 1), "title": title})


# ---------------------------------------------------------------- offline stand-ins (ESSAY_STUDIO_FAKE_LLM=1)
def _sentences(essay: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", plain_text(essay)) if len(s.split()) > 3] or ["(empty)"]


def _fake_summary(essay: str, style: str) -> str:
    s = _sentences(essay)
    if style == "key-points":
        return s[0] + "\n\n" + "\n".join(f"- {x}" for x in s[1:6])
    return " ".join(s[:2 if style == "tldr" else 5])


def _fake_podcast(essay: str, a: str, b: str) -> str:
    s = _sentences(essay)
    lines = [f"{a}:\nWelcome back to the deep dive. Today we're looking at an essay that opens like this: {s[0]}",
             f"{b}:\nAnd I have to say, I wasn't sure about it at first. But it grows on you."]
    for i, x in enumerate(s[1:7]):
        lines.append(f"{a if i % 2 else b}:\nHere's a key line: {x} What do you make of that?")
    lines.append(f"{b}:\nThat's the takeaway for me. What would you add? Let us know.")
    return "\n\n".join(lines)


def _fake_storyboard(project: dict) -> dict:
    s = _sentences(project["essay"])
    return {"title": project["title"], "subtitle": "An explainer (offline test mode)",
            "intro": f"Let's walk through {project['title']}.",
            "slides": [{"heading": f"Point {i + 1}", "bullets": [x[:60], "Why it matters"], "narration": x}
                       for i, x in enumerate(s[:4])]}
