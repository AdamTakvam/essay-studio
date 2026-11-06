"""Runtime configuration, read from environment variables (and an optional .env file)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR = Path(os.environ.get("ESSAY_STUDIO_DATA", ROOT / "data")).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
TTS_CACHE_DIR = DATA_DIR / "tts-cache"
WEB_DIR = ROOT / "web"

# Test switches: replace Claude / edge-tts with offline stand-ins.
FAKE_LLM = os.environ.get("ESSAY_STUDIO_FAKE_LLM") == "1"
FAKE_TTS = os.environ.get("ESSAY_STUDIO_FAKE_TTS") == "1"

# edge-tts uses aiohttp, which ignores HTTPS_PROXY unless told about it.
TTS_PROXY = os.environ.get("EDGE_TTS_PROXY") or os.environ.get("HTTPS_PROXY") or None

# Same retry/transition behaviour as render-deepdive.ps1.
TTS_MAX_ATTEMPTS = 5
TTS_TRANSITION_SECONDS = 0.25

for d in (PROJECTS_DIR, TTS_CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
