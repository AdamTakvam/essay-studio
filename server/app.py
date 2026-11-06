"""HTTP API + static web UI."""
import asyncio
import json
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm, media, publish, store, tts
from .config import FAKE_LLM, FAKE_TTS, WEB_DIR
from .logbus import bus, logger

log = logger("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.bind(asyncio.get_running_loop())
    for tool in ("ffmpeg",):
        if not shutil.which(tool):
            log(f"{tool} was not found in PATH — audio/video generation will fail", "error")
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        log("edge-tts is not installed (pip install edge-tts)", "error")
    if FAKE_LLM or FAKE_TTS:
        log(f"Offline test mode: fake Claude={FAKE_LLM}, fake TTS={FAKE_TTS}", "warn")
    # Jobs that were running when the server stopped can't resume their task; mark them.
    for p in store.list_projects():
        def fix(d):
            for k, v in d.get("media", {}).items():
                if v.get("status") in ("queued", "running"):
                    v.update(status="error", message="Interrupted (server restarted) — generate again; "
                                                    "finished speech turns are cached")
            for r in d.get("publish", {}).get("runs", []):
                if r.get("status") == "running":
                    r["status"] = "error"
        await store.update(p["id"], fix)
    log("Essay Studio ready")
    yield


app = FastAPI(title="Essay Studio", lifespan=lifespan)


def _project(pid: str) -> dict:
    try:
        return store.load(pid)
    except KeyError:
        raise HTTPException(404, "Project not found")


# ---------------------------------------------------------------- tool output stream
@app.get("/api/events")
async def events(request: Request):
    q, history = bus.subscribe()

    async def gen():
        try:
            for e in history:
                yield f"data: {json.dumps(e)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(e)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- catalogue
@app.get("/api/models")
async def models():
    return await llm.list_models()


@app.get("/api/voices")
async def voices():
    return await tts.list_voices()


@app.get("/api/platforms")
async def platforms():
    return [{"id": k, **v} for k, v in publish.PLATFORMS.items()]


# ---------------------------------------------------------------- projects
class ProjectPatch(BaseModel):
    essay: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    title: str | None = None
    stage: str | None = None


@app.get("/api/projects")
async def projects():
    return store.list_projects()


@app.post("/api/projects")
async def create_project():
    ms = await llm.list_models()
    p = store.new_project(ms[0]["id"] if ms else "")
    log(f"Created project {p['id']}")
    return p


@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    return _project(pid)


@app.put("/api/projects/{pid}")
async def save_project(pid: str, patch: ProjectPatch):
    _project(pid)
    changed = {}

    def fn(d):
        if patch.essay is not None and patch.essay != d["essay"]:
            d["essay"] = patch.essay
            d["essay_hash"] = store.essay_hash(patch.essay)
            changed["essay"] = len(patch.essay.split())
            if not d.get("title_locked"):
                d["title"] = store.derive_title(patch.essay)
        if patch.title is not None and patch.title.strip():
            d["title"], d["title_locked"] = patch.title.strip(), True
        for f in ("system_prompt", "model"):
            if getattr(patch, f) is not None:
                d[f] = getattr(patch, f)
        if patch.stage in store.STAGES:
            d["stage"] = patch.stage
    d = await store.update(pid, fn)
    if "essay" in changed:
        log(f"Saved essay ({changed['essay']:,} words) → {store.project_dir(pid) / 'essay.md'}")
    return d


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    _project(pid)
    store.delete_project(pid)
    log(f"Deleted project {pid}")
    return {"ok": True}


@app.get("/api/projects/{pid}/files/{rel:path}")
async def project_file(pid: str, rel: str, download: bool = False):
    try:
        f = store.safe_file(pid, rel)
    except KeyError:
        raise HTTPException(404, "File not found")
    return FileResponse(f, filename=f.name if download else None)


# ---------------------------------------------------------------- writing assistant
class AssistReq(BaseModel):
    text: str
    system_prompt: str = ""
    model: str


ASSIST_SYSTEM = """You are a writing partner working beside the author in real time.
The author is drafting an essay; their current draft (possibly rough notes or half-finished) is in <draft>.
Write the best version of the essay they are trying to write: keep their ideas, argument and point of view,
develop them into finished prose, fix structure and flow, and fill obvious gaps — but don't add claims
they would not make. If the draft is only a few words, write a short opening that takes the idea forward.
Output only the essay text (markdown headings allowed), no commentary or preamble."""


@app.post("/api/assist")
async def assist(req: AssistReq):
    system = ASSIST_SYSTEM + (f"\n\nStyle instructions from the author:\n{req.system_prompt.strip()}"
                              if req.system_prompt.strip() else "")

    def fake():
        return "\n\n".join(p.strip().capitalize() + ("" if p.strip().endswith(".") else ".")
                           for p in req.text.split("\n\n") if p.strip()) + "\n\n(Polished in offline test mode.)"

    async def gen():
        try:
            async for chunk in llm.stream(req.model, system, f"<draft>\n{req.text}\n</draft>",
                                          label="writing assistant", max_tokens=8000, fake=fake):
                yield chunk
        except Exception as e:
            yield f"\n\n⚠ {type(e).__name__}: {e}"
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------- media
@app.post("/api/projects/{pid}/media/{kind}")
async def start_media(pid: str, kind: str, request: Request):
    _project(pid)
    options = await request.json() if (await request.body()) else {}
    try:
        return await media.start(pid, kind, options)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{pid}/media/{kind}/cancel")
async def cancel_media(pid: str, kind: str):
    return {"cancelled": await media.cancel(pid, kind)}


class Approve(BaseModel):
    approved: bool


@app.post("/api/projects/{pid}/media/{kind}/approve")
async def approve_media(pid: str, kind: str, body: Approve):
    p = _project(pid)
    m = p["media"].get(kind)
    if not m or m.get("status") != "done":
        raise HTTPException(400, "Only finished media can be approved")

    def fn(d):
        d["media"][kind]["approved"] = body.approved
    d = await store.update(pid, fn)
    log(f"{kind}: {'approved for publication' if body.approved else 'approval withdrawn'}", "info")
    bus.emit("job", project=pid, media=kind, state=d["media"][kind])
    return d["media"][kind]


@app.delete("/api/projects/{pid}/media/{kind}")
async def discard_media(pid: str, kind: str):
    await media.cancel(pid, kind)
    shutil.rmtree(store.project_dir(pid) / "media" / kind, ignore_errors=True)
    await store.update(pid, lambda d: d["media"].pop(kind, None))
    bus.emit("job", project=pid, media=kind, state=None)
    log(f"{kind}: discarded")
    return {"ok": True}


# ---------------------------------------------------------------- publish
class PublishReq(BaseModel):
    platforms: list[str]


@app.post("/api/projects/{pid}/publish")
async def do_publish(pid: str, req: PublishReq):
    _project(pid)
    try:
        return await publish.start(pid, req.platforms)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- web
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")
