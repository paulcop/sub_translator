"""Stockage en mémoire, avec expiration. Rien ne touche le disque."""

import secrets
import threading
import time
from dataclasses import dataclass, field

from .subtitles import Track

TTL_SECONDS = 30 * 60


@dataclass
class Upload:
    filename: str
    tracks: list[Track]
    created: float = field(default_factory=time.time)


@dataclass
class Job:
    filename: str
    status: str = "running"  # running | done | error | cancelled
    done: int = 0
    total: int = 0
    last_source: str = ""
    last_target: str = ""
    removed_sdh: int = 0
    error: str | None = None
    result: bytes | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    created: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "last_source": self.last_source,
            "last_target": self.last_target,
            "removed_sdh": self.removed_sdh,
            "error": self.error,
            "filename": self.filename,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class Store:
    def __init__(self):
        self._items: dict[str, object] = {}
        self._lock = threading.Lock()

    def put(self, item) -> str:
        key = secrets.token_urlsafe(16)
        with self._lock:
            self._items[key] = item
        return key

    def get(self, key: str):
        with self._lock:
            return self._items.get(key)

    def pop(self, key: str):
        with self._lock:
            return self._items.pop(key, None)

    def purge(self):
        limit = time.time() - TTL_SECONDS
        with self._lock:
            for key in [k for k, v in self._items.items() if v.created < limit]:
                del self._items[key]


uploads = Store()
jobs = Store()


def start_janitor():
    def loop():
        while True:
            time.sleep(60)
            uploads.purge()
            jobs.purge()

    threading.Thread(target=loop, daemon=True).start()
