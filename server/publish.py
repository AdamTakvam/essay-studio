"""Bundles the essay + approved media into per-platform packages.

Direct posting needs each platform's developer app + OAuth, so for now PUBLISH builds
upload-ready packages (correct aspect ratios, caption limits, thread splits) and a zip
per platform. Each package's manifest.json records what a real uploader would send."""
import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from . import llm, slides, store, tts
from .logbus import bus, logger
from .media import plain_text

log = logger("publisher")

PLATFORMS = {
    "youtube": {"name": "YouTube", "aspect": "16:9", "max_video": None,
                "text": "Title ≤100 chars, description ≤5,000", "audio_as_video": True},
    "tiktok": {"name": "TikTok", "aspect": "9:16", "max_video": 600, "text": "Caption ≤2,200 chars"},
    "instagram": {"name": "Instagram", "aspect": "9:16", "max_video": 180,
                  "text": "Caption ≤2,200 chars · Reel + slide carousel"},
    "facebook": {"name": "Facebook", "aspect": "16:9", "max_video": None, "text": "Post text (long-form OK)",
                 "audio_as_video": True},
    "x": {"name": "X", "aspect": "16:9", "max_video": 140, "text": "Thread of ≤280-char posts"},
    "linkedin": {"name": "LinkedIn", "aspect": "16:9", "max_video": 600, "text": "Post ≤3,000 chars"},
}
LIMITS = {"youtube.title": 100, "youtube.description": 5000, "tiktok.caption": 2200,
          "instagram.caption": 2200, "facebook.post": 60000, "linkedin.post": 3000, "x.post": 280}

_task: dict[str, asyncio.Task] = {}


def approved_media(project: dict) -> dict:
    return {k: v for k, v in project.get("media", {}).items()
            if v.get("approved") and v.get("status") == "done"}


async def start(pid: str, platforms: list[str]) -> dict:
    platforms = [p for p in platforms if p in PLATFORMS]
    if not platforms:
        raise ValueError("Pick at least one platform")
    if pid in _task and not _task[pid].done():
        raise RuntimeError("A publish run is already in progress")
    project = store.load(pid)
    run = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], "started": time.time(),
           "finished": None, "status": "running", "platforms": platforms,
           "included": sorted(approved_media(project)), "results": {}}
    await _save(pid, run)
    _task[pid] = asyncio.create_task(_run(pid, project, run))
    return run


async def _save(pid: str, run: dict) -> None:
    def fn(d):
        runs = d["publish"].setdefault("runs", [])
        for i, r in enumerate(runs):
            if r["id"] == run["id"]:
                runs[i] = run
                break
        else:
            runs.append(run)
        d["publish"]["runs"] = runs[-10:]
    await store.update(pid, fn)
    bus.emit("publish", project=pid, run=run)


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


async def _copy_text(project: dict, platforms: list[str], media: dict, summary: str) -> dict:
    wanted = {
        "youtube": '"youtube": {"title": str, "description": str, "tags": [str]}',
        "tiktok": '"tiktok": {"caption": str}',
        "instagram": '"instagram": {"caption": str}',
        "facebook": '"facebook": {"post": str}',
        "x": '"x": {"thread": [str]}',
        "linkedin": '"linkedin": {"post": str}',
    }
    spec = "{" + ", ".join(wanted[p] for p in platforms) + "}"
    have = ", ".join(sorted(media)) or "no media (text only)"
    prompt = f"""Write social media copy to publish this essay. Approved media being attached: {have}.
Limits: YouTube title ≤100 chars, description ≤5000; TikTok & Instagram captions ≤2200 with 3–6 relevant hashtags at the end;
LinkedIn ≤3000; X: a thread of 3–8 posts, each ≤270 characters, the first one a strong hook.
Facebook: when there is no video, the post can carry the full essay text with a short intro line.
Match each platform's tone. Do not invent claims beyond the essay.
Reply with JSON only, matching: {spec}

<essay title="{project['title']}">
{project['essay']}
</essay>
""" + (f"\n<summary>\n{summary}\n</summary>" if summary else "")
    def fake():
        s = summary or plain_text(project["essay"])[:400]
        return json.dumps({"youtube": {"title": project["title"], "description": s, "tags": ["essay"]},
                           "tiktok": {"caption": _clip(s, 200) + " #essay"},
                           "instagram": {"caption": _clip(s, 400) + " #essay #ideas"},
                           "facebook": {"post": project["essay"]},
                           "x": {"thread": [_clip(x, 270) for x in re.split(r"(?<=[.!?])\s+", s)[:4]]},
                           "linkedin": {"post": _clip(s, 1200)}})
    raw = await llm.complete(project.get("model") or "claude-sonnet-4-5",
                             "You are a social media editor. Reply with JSON only.", prompt,
                             label="publish: platform copy", max_tokens=8000, fake=fake)
    return llm.parse_json(raw)


async def _vertical(src: Path, out: Path) -> Path:
    """16:9 → 9:16 with a blurred fill, for TikTok / Reels."""
    if not out.exists():
        log(f"Making 9:16 version of {src.name}…")
        await tts.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-filter_complex",
                       "[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                       "boxblur=30:2,eq=brightness=-0.15[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
                       "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy",
                       "-movflags", "+faststart", str(out)])
    return out


async def _audiogram(audio: Path, title: str, subtitle: str, out: Path, work: Path) -> Path:
    if not out.exists():
        card = await asyncio.to_thread(slides.render_card, work / "audiogram_card.png", title, subtitle)
        log(f"Making audio-with-title-card video from {audio.name}…")
        await tts.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "2", "-i", str(card),
                       "-i", str(audio), "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
                       "-pix_fmt", "yuv420p", "-r", "2", "-c:a", "copy", "-shortest", "-movflags", "+faststart",
                       str(out)])
    return out


async def _run(pid: str, project: dict, run: dict) -> None:
    base = store.project_dir(pid)
    root = base / "publish" / run["id"]
    work = root / "_work"
    work.mkdir(parents=True, exist_ok=True)
    media = approved_media(project)
    log(f"Publishing “{project['title']}” to {', '.join(PLATFORMS[p]['name'] for p in run['platforms'])}")
    log(f"Included: essay text" + (", " + ", ".join(sorted(media)) if media else " only (no approved media)"))
    try:
        summary = ""
        if "summary" in media:
            summary = (base / media["summary"]["files"]["text"]).read_text(encoding="utf-8")
        for p in run["platforms"]:
            run["results"][p] = {"status": "queued", "files": [], "warnings": []}
        await _save(pid, run)

        copy = await _copy_text(project, run["platforms"], media, summary)
        video = base / media["video"]["files"]["video"] if "video" in media else None
        video_dur = media.get("video", {}).get("meta", {}).get("duration")
        audio_key = "podcast" if "podcast" in media else ("audiobook" if "audiobook" in media else None)
        audio = base / media[audio_key]["files"]["audio"] if audio_key else None

        for p in run["platforms"]:
            spec, res = PLATFORMS[p], run["results"][p]
            res["status"] = "packaging"
            await _save(pid, run)
            d = root / p
            d.mkdir(parents=True, exist_ok=True)
            files: list[str] = []

            def add(src: Path, name: str):
                shutil.copy(src, d / name)
                files.append(name)

            (d / "essay.md").write_text(project["essay"], encoding="utf-8")
            files.append("essay.md")
            if summary:
                (d / "summary.md").write_text(summary, encoding="utf-8")
                files.append("summary.md")

            c = copy.get(p, {}) if isinstance(copy, dict) else {}
            post: dict = {}
            if p == "youtube":
                post = {"title": _clip(c.get("title") or project["title"], LIMITS["youtube.title"]),
                        "description": _clip(c.get("description", summary), LIMITS["youtube.description"]),
                        "tags": c.get("tags", [])}
            elif p == "x":
                thread = [_clip(t, LIMITS["x.post"]) for t in c.get("thread", []) if t.strip()]
                post = {"thread": thread or [_clip(project["title"], 280)]}
            else:
                field = "caption" if p in ("tiktok", "instagram") else "post"
                post = {field: _clip(c.get(field, summary or project["essay"]), LIMITS[f"{p}.{field}"])}
            (d / "post.json").write_text(json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
            (d / "post.txt").write_text(
                "\n\n".join(f"[{i + 1}/{len(post['thread'])}] {t}" for i, t in enumerate(post["thread"]))
                if "thread" in post else "\n\n".join(
                    (f"{k.upper()}:\n{', '.join(v) if isinstance(v, list) else v}") for k, v in post.items()),
                encoding="utf-8")
            files += ["post.json", "post.txt"]

            # ---- media
            if video:
                if spec["aspect"] == "9:16":
                    add(await _vertical(video, work / "video_9x16.mp4"), "video.mp4")
                else:
                    add(video, "video.mp4")
                add(base / media["video"]["files"]["thumbnail"], "thumbnail.png")
                if spec["max_video"] and video_dur and video_dur > spec["max_video"]:
                    res["warnings"].append(f"Video is {video_dur:.0f}s; {spec['name']} allows {spec['max_video']}s "
                                           f"— trim before uploading")
                if p == "instagram":
                    for i, s in enumerate(sorted((base / "media" / "video" / "slides").glob("*.png"))[:10]):
                        add(s, f"carousel_{i + 1:02d}.png")
            elif audio and spec.get("audio_as_video"):
                vid = await _audiogram(audio, project["title"],
                                       "The podcast" if audio_key == "podcast" else "The audiobook",
                                       work / f"{audio_key}_audiogram.mp4", work)
                add(vid, "video.mp4")
            if audio and p in ("youtube", "facebook", "linkedin"):
                add(audio, f"{audio_key}.m4a")
            if not video and not audio and p in ("tiktok", "instagram"):
                res["warnings"].append(f"{spec['name']} needs an image or video; only text is in this package")

            manifest = {"platform": spec["name"], "project": project["title"], "built": time.ctime(),
                        "post": post, "files": files, "warnings": res["warnings"],
                        "posting": "not configured — upload these files manually or add an API uploader"}
            (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            files.append("manifest.json")
            zip_base = root / (re.sub(r"[^\w.-]+", "_", project["title"])[:40].strip("_") + f"-{p}")
            zip_path = Path(await asyncio.to_thread(shutil.make_archive, str(zip_base), "zip", d))
            res.update(status="packaged", files=[(d / f).relative_to(base).as_posix() for f in files],
                       zip=zip_path.relative_to(base).as_posix(), post=post)
            log(f"{spec['name']}: package ready ({len(files)} files, {zip_path.stat().st_size / 1e6:.1f} MB)")
            for w in res["warnings"]:
                log(f"{spec['name']}: {w}", "warn")
            log(f"{spec['name']}: direct posting is not configured — package saved for upload")
            await _save(pid, run)
        run.update(status="done", finished=time.time())
        shutil.rmtree(work, ignore_errors=True)
        log(f"Publish run {run['id']} complete → {root}")
    except Exception as e:
        log(f"Publish failed: {type(e).__name__}: {e}", "error")
        run.update(status="error", error=str(e), finished=time.time())
    await _save(pid, run)
