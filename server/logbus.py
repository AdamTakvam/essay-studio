"""In-process event bus. Every tool (Claude, edge-tts, ffmpeg, publisher) reports here,
and the browser's tool-output pane subscribes over Server-Sent Events."""
import asyncio
import threading
import time
from collections import deque


class LogBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._history: deque = deque(maxlen=500)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _publish(self, event: dict) -> None:
        if event["kind"] == "log":
            self._history.append(event)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def emit(self, kind: str, **payload) -> None:
        event = {"kind": kind, "t": time.time(), **payload}
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._publish(event)
        else:  # called from a worker thread
            loop.call_soon_threadsafe(self._publish, event)

    def log(self, source: str, msg: str, level: str = "info") -> None:
        for line in str(msg).splitlines() or [""]:
            self.emit("log", source=source, level=level, msg=line)
        print(f"[{source}] {msg}", flush=True)

    def subscribe(self) -> tuple[asyncio.Queue, list]:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._subs.add(q)
        return q, list(self._history)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


bus = LogBus()


def logger(source: str):
    """Return a small callable bound to one source name: log("message", level=...)."""
    def _log(msg: str, level: str = "info") -> None:
        bus.log(source, msg, level)
    return _log
