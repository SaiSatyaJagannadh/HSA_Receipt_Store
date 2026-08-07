"""Small shared helpers: hashing, slugs, retry, append-only audit log."""

import functools
import hashlib
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_hash(file_hash: str) -> str:
    return file_hash[:8]


_SLUG_STRIP = re.compile(r"[^A-Za-z0-9]+")


def slugify(text: str, maxlen: int = 40) -> str:
    """Provider -> filename-safe token. 'CVS Pharmacy #421' -> 'CVS_Pharmacy_421'."""
    cleaned = _SLUG_STRIP.sub("_", (text or "").strip()).strip("_")
    return (cleaned[:maxlen].rstrip("_")) or "Unknown"


def retry(tries: int = 5, base: float = 1.0, cap: float = 30.0, exceptions=(Exception,)):
    """Exponential backoff with jitter. Wraps every Google/NVIDIA network call."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for attempt in range(tries):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203 - retry loop
                    last = exc
                    if attempt == tries - 1:
                        break
                    delay = min(base * (2**attempt), cap) + random.uniform(0, 0.5)
                    audit("retry", fn=fn.__name__, attempt=attempt + 1, error=str(exc)[:300])
                    time.sleep(delay)
            raise last

        return wrapper

    return deco


_audit_path: Path | None = None


def set_audit_path(path: Path) -> None:
    global _audit_path
    _audit_path = path
    path.parent.mkdir(parents=True, exist_ok=True)


def audit(event: str, **fields) -> None:
    """Append-only local log. Every write to Drive/Sheets lands here first."""
    if _audit_path is None:
        return
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with _audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
