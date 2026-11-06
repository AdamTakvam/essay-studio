"""Projects live on disk: data/projects/<id>/project.json + essay.md + media/ + publish/."""
import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from .config import PROJECTS_DIR

MEDIA_KINDS = ("summary", "audiobook", "podcast", "video")
STAGES = ("write", "media", "publish")

_locks: dict[str, asyncio.Lock] = {}


def lock_for(pid: str) -> asyncio.Lock:
    return _locks.setdefault(pid, asyncio.Lock())


def essay_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def project_dir(pid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", pid):
        raise KeyError(pid)
    return PROJECTS_DIR / pid


def _path(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def derive_title(essay: str) -> str:
    for line in essay.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return (line[:77] + "…") if len(line) > 78 else line
    return "Untitled essay"


def new_project(model: str = "") -> dict:
    pid = uuid.uuid4().hex[:12]
    now = time.time()
    data = {
        "id": pid,
        "title": "Untitled essay",
        "title_locked": False,
        "created": now,
        "updated": now,
        "essay": "",
        "essay_hash": essay_hash(""),
        "system_prompt": "Write in clear, vivid, conversational prose. Keep my voice and my argument; "
                         "tighten structure and fill gaps.",
        "model": model,
        "stage": "write",
        "media": {},
        "publish": {"runs": []},
    }
    project_dir(pid).mkdir(parents=True, exist_ok=True)
    save(data)
    return data


def load(pid: str) -> dict:
    p = _path(pid)
    if not p.exists():
        raise KeyError(pid)
    return json.loads(p.read_text(encoding="utf-8"))


def save(data: dict) -> dict:
    data["updated"] = time.time()
    d = project_dir(data["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "project.json.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(d / "project.json")
    (d / "essay.md").write_text(data.get("essay", ""), encoding="utf-8")
    return data


async def update(pid: str, fn) -> dict:
    """Read-modify-write a project under its lock. fn mutates the dict in place."""
    async with lock_for(pid):
        data = load(pid)
        fn(data)
        return save(data)


def list_projects() -> list[dict]:
    out = []
    for p in PROJECTS_DIR.glob("*/project.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "title": d["title"], "updated": d["updated"], "stage": d["stage"]})
        except Exception:
            continue
    return sorted(out, key=lambda x: x["updated"], reverse=True)


def delete_project(pid: str) -> None:
    shutil.rmtree(project_dir(pid), ignore_errors=True)


def safe_file(pid: str, rel: str) -> Path:
    base = project_dir(pid).resolve()
    target = (base / rel).resolve()
    if base not in target.parents or not target.is_file():
        raise KeyError(rel)
    return target
