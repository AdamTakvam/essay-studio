"""Speech rendering — a Python port of render-deepdive.ps1.

Technique (unchanged from the PowerShell script):
  * each speaker turn is one edge-tts request (voice + rate per speaker)
  * up to 5 attempts per turn with 3s/6s/12s/24s backoff
  * every turn is normalised to 24 kHz mono 16-bit WAV with ffmpeg
  * turns are joined with a short silence through ffmpeg's concat demuxer
  * the final mix is AAC 128k in an .m4a container
  * a build cache keyed by SHA-256(transcript + voices + rates + transition)
    lets an interrupted render resume, and any change gets its own cache
"""
import asyncio
import hashlib
import json
import re
import shutil
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import FAKE_TTS, TTS_CACHE_DIR, TTS_MAX_ATTEMPTS, TTS_PROXY, TTS_TRANSITION_SECONDS
from .logbus import logger

log_tts = logger("edge-tts")
log_ff = logger("ffmpeg")

FALLBACK_VOICES = [
    {"id": v, "locale": v[:5], "gender": g, "name": v.split("-")[2].replace("Neural", "")}
    for v, g in [
        ("en-US-AndrewNeural", "Male"), ("en-US-EmmaNeural", "Female"), ("en-US-AvaNeural", "Female"),
        ("en-US-BrianNeural", "Male"), ("en-US-AriaNeural", "Female"), ("en-US-GuyNeural", "Male"),
        ("en-US-JennyNeural", "Female"), ("en-US-ChristopherNeural", "Male"), ("en-GB-SoniaNeural", "Female"),
        ("en-GB-RyanNeural", "Male"), ("en-AU-NatashaNeural", "Female"), ("en-AU-WilliamNeural", "Male"),
    ]
]
_voice_cache: list | None = None


async def list_voices() -> list[dict]:
    global _voice_cache
    if _voice_cache:
        return _voice_cache
    if FAKE_TTS:
        return FALLBACK_VOICES
    try:
        import edge_tts
        raw = await edge_tts.list_voices(proxy=TTS_PROXY)
        voices = [
            {"id": v["ShortName"], "locale": v["Locale"], "gender": v.get("Gender", ""),
             "name": v["ShortName"].split("-", 2)[-1].replace("Neural", "").replace("Multilingual", " Multilingual")}
            for v in raw if v["Locale"].startswith("en-")
        ]
        voices.sort(key=lambda v: (v["locale"] != "en-US", v["locale"], v["name"]))
        _voice_cache = voices
        log_tts(f"Loaded {len(voices)} English voices")
        return voices
    except Exception as e:
        log_tts(f"Could not fetch voice list ({e}); using built-in list", "warn")
        return FALLBACK_VOICES


def rate_str(percent: int | float) -> str:
    p = int(round(float(percent)))
    return f"{'+' if p >= 0 else ''}{p}%"


@dataclass
class Turn:
    speaker: str
    text: str


@dataclass
class RenderResult:
    output: Path | None
    segments: list[Path]          # one WAV per turn, in order
    durations: list[float]        # seconds per turn
    cache_dir: Path
    rendered_now: int
    reused: int


def parse_transcript(content: str, speakers: list[str]) -> list[Turn]:
    """Same regex as the PowerShell script: '^(A|B):\\s*(.*?)(?=^(?:A|B):|\\z)'."""
    names = "|".join(re.escape(s) for s in speakers)
    pattern = re.compile(rf"^({names}):\s*(.*?)(?=^(?:{names}):|\Z)", re.M | re.S)
    turns = [Turn(m.group(1), m.group(2).strip()) for m in pattern.finditer(content)]
    return [t for t in turns if t.text]


async def run(cmd: list[str], log=log_ff) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        if text:
            log(text[-2000:], "error")
        raise RuntimeError(f"{Path(cmd[0]).name} exited with code {proc.returncode}")
    if text:
        log(text[-1000:])


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _ok(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 1024
    except OSError:
        return False


async def _synthesize(text: str, voice: str, rate: str, mp3: Path) -> None:
    if FAKE_TTS:  # offline stand-in: a soft tone as long as the speech would be
        secs = max(1.0, len(text.split()) / 2.6)
        freq = 220 + (sum(map(ord, voice)) % 200)
        await run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                   f"sine=frequency={freq}:duration={secs:.2f}", "-af", "volume=0.15",
                   "-c:a", "libmp3lame", "-b:a", "48k", str(mp3)])
        return
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, proxy=TTS_PROXY)
    await communicate.save(str(mp3))


async def render(turns: list[Turn], voices: dict[str, dict], output: Path | None, *, label: str,
                 transition: float = TTS_TRANSITION_SECONDS, fresh: bool = False,
                 progress: Callable[[float, str], None] | None = None) -> RenderResult:
    """voices = {SPEAKER: {"voice": "en-US-AndrewNeural", "rate": "+5%"}}.
    If output is None, only the per-turn WAVs are produced (used by the video pipeline)."""
    if not turns:
        raise ValueError("Nothing to speak: transcript has no turns")
    for t in turns:
        if t.speaker not in voices:
            raise ValueError(f"No voice configured for speaker {t.speaker}")

    # ---- build-cache identity (transcript + voices + rates + transition) ----
    fingerprint = "\n\n".join(f"{t.speaker}:\n{t.text}" for t in turns) + "\n\n" + "\n".join(
        f"{s}_VOICE={v['voice']}\n{s}_RATE={v['rate']}" for s, v in sorted(voices.items())
    ) + f"\nTRANSITION_SECONDS={transition}" + ("\nFAKE=1" if FAKE_TTS else "")
    build_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    work = TTS_CACHE_DIR / f"{safe}-{build_hash}"
    if fresh and work.exists():
        log_tts(f"Removing cached build {work.name}")
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "build.json").write_text(json.dumps({
        "label": label, "build_hash": build_hash, "turns": len(turns),
        "voices": voices, "transition_seconds": transition}, indent=2), encoding="utf-8")

    total = len(turns)
    wavs = [work / f"turn_{i + 1:04d}.wav" for i in range(total)]
    cached = sum(1 for w in wavs if _ok(w))
    pending = total - cached

    log_tts("=" * 40)
    log_tts(f" Rendering: {label}")
    log_tts(f" Turns {total} · build {build_hash}")
    for s, v in voices.items():
        log_tts(f" {s:<10} {v['voice']} @ {v['rate']}")
    log_tts(f" Cached {cached} · remaining {pending}" if cached else " Starting a new render.")
    log_tts("=" * 40)

    silence = work / "silence.wav"
    if transition > 0 and not _ok(silence) and not silence.exists():
        await run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                   "-t", str(transition), "-c:a", "pcm_s16le", str(silence)])

    started = time.time()
    rendered_now = 0
    for i, turn in enumerate(turns):
        n = i + 1
        wav, mp3, txt = wavs[i], work / f"turn_{n:04d}.mp3", work / f"turn_{n:04d}.txt"
        pct = n / total
        if _ok(wav):
            if progress:
                progress(pct * 0.95, f"Turn {n}/{total} (cached)")
            continue

        # ETA from turns rendered during THIS invocation only (cached turns don't distort it)
        if rendered_now:
            per = (time.time() - started) / rendered_now
            eta = time.strftime("%H:%M:%S", time.gmtime(max(0, per * (pending - rendered_now))))
        else:
            eta = "calculating…"
        elapsed = time.strftime("%H:%M:%S", time.gmtime(time.time() - started))
        log_tts(f"[{n}/{total}] {turn.speaker:<8} {int(pct * 100):3d}%  elapsed {elapsed}  ETA {eta}")
        if progress:
            progress((i / total) * 0.95, f"Speaking turn {n}/{total} ({turn.speaker}) · ETA {eta}")

        txt.write_text(turn.text, encoding="utf-8")
        v = voices[turn.speaker]
        for attempt in range(1, TTS_MAX_ATTEMPTS + 1):
            mp3.unlink(missing_ok=True)
            if attempt > 1:
                log_tts(f"          Retry {attempt}/{TTS_MAX_ATTEMPTS}")
            try:
                await _synthesize(turn.text, v["voice"], v["rate"], mp3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_tts(f"          {type(e).__name__}: {e}", "warn")
            if _ok(mp3):
                break
            if attempt < TTS_MAX_ATTEMPTS:
                delay = 3 * 2 ** (attempt - 1)
                log_tts(f"          No audio received. Retrying in {delay}s…", "warn")
                await asyncio.sleep(delay)
        else:
            raise RuntimeError(f"edge-tts failed on turn {n} ({turn.speaker}) after {TTS_MAX_ATTEMPTS} attempts")

        wav.unlink(missing_ok=True)
        try:
            await run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp3),
                       "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        except Exception:
            wav.unlink(missing_ok=True)
            raise
        if not _ok(wav):
            raise RuntimeError(f"Turn {n} produced an invalid WAV file")
        rendered_now += 1

    log_tts(f"Rendering complete. Rendered now {rendered_now} · reused cache {cached}")
    durations = [wav_seconds(w) for w in wavs]

    if output is not None:
        if progress:
            progress(0.97, "Assembling final audio…")
        concat = work / "concat.txt"
        lines = []
        for i, w in enumerate(wavs):
            lines.append(f"file '{w.as_posix()}'")
            if transition > 0 and i < total - 1:
                lines.append(f"file '{silence.as_posix()}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        log_ff(f"Assembling {output.name} (AAC 128k)…")
        await run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                   "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)])
        total_secs = sum(durations) + transition * (total - 1)
        log_ff(f"Wrote {output.name} · {time.strftime('%H:%M:%S', time.gmtime(total_secs))}")

    return RenderResult(output, wavs, durations, work, rendered_now, cached)
