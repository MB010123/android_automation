"""Wake the panel and pre-open GADS MJPEG so GET /stream is not a cold start.

The 35–40s Slot 21 first-frame stall was Farm ``device_screen_prepare`` /
``device_access_mode`` blocking the HTTP response and the MJPEG GET. This
module runs KEYCODE_WAKEUP + a GADS MJPEG open in the background as soon as
a live session is created, then hands the already-open upstream to the
browser GET. Full screen-prepare stays in the background after the first
JPEG is cached.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from application.device_stream_wake import request_device_stream_wake

logger = logging.getLogger("vps_backend.stream_warmup")

MJPEG_CHUNK_SIZE = 4096
PRIME_WAIT_SECONDS = 5.0
PRIME_TTL_SECONDS = 15.0


class PrimedMjpeg:
    """requests-like MJPEG upstream that already has the first chunk."""

    def __init__(self, upstream: Any, chunks: Any) -> None:
        self._upstream = upstream
        self._chunks = chunks
        self.status_code = int(getattr(upstream, "status_code", 200) or 200)
        self.headers = getattr(upstream, "headers", {}) or {}
        self.device_id = getattr(upstream, "device_id", None)

    def iter_content(self, chunk_size: int = MJPEG_CHUNK_SIZE):
        _ = chunk_size
        yield from self._chunks

    def close(self) -> None:
        closer = getattr(self._upstream, "close", None)
        if callable(closer):
            closer()


class MjpegPrimeCache:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._items: dict[str, tuple[float, PrimedMjpeg]] = {}

    def begin(self, device_id: str) -> None:
        key = str(device_id or "").strip()
        if not key:
            return
        event = threading.Event()
        with self._lock:
            previous = self._pending.get(key)
            old = self._items.pop(key, None)
            self._pending[key] = event
        if old is not None:
            old[1].close()
        if previous is not None:
            previous.set()

    def store(self, device_id: str, stream: PrimedMjpeg) -> None:
        key = str(device_id or "").strip()
        if not key:
            stream.close()
            return
        with self._lock:
            event = self._pending.get(key)
            old = self._items.pop(key, None)
            self._items[key] = (self._clock(), stream)
        if old is not None:
            old[1].close()
        if event is not None:
            event.set()

    def fail(self, device_id: str) -> None:
        key = str(device_id or "").strip()
        with self._lock:
            event = self._pending.pop(key, None)
            old = self._items.pop(key, None)
        if old is not None:
            old[1].close()
        if event is not None:
            event.set()

    def take(self, device_id: str, *, wait_seconds: float = PRIME_WAIT_SECONDS) -> PrimedMjpeg | None:
        key = str(device_id or "").strip()
        if not key:
            return None
        with self._lock:
            event = self._pending.get(key)
        if event is not None and wait_seconds > 0:
            event.wait(wait_seconds)
        with self._lock:
            item = self._items.pop(key, None)
            self._pending.pop(key, None)
        if item is None:
            return None
        stored_at, stream = item
        if self._clock() - stored_at > PRIME_TTL_SECONDS:
            stream.close()
            return None
        return stream

    def prime_from_platform(
        self,
        platform: Any,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
    ) -> bool:
        opener = getattr(platform, "open_mjpeg_stream", None)
        if not callable(opener):
            self.fail(device_id)
            return False
        try:
            upstream = opener(
                device_id=device_id,
                platform_username=platform_username,
                platform_password=platform_password,
            )
        except Exception:
            logger.warning("mjpeg_prime_open_failed device=%s", device_id)
            self.fail(device_id)
            return False
        if upstream is None or int(getattr(upstream, "status_code", 0) or 0) != 200:
            closer = getattr(upstream, "close", None)
            if callable(closer):
                closer()
            self.fail(device_id)
            return False
        iterator = getattr(upstream, "iter_content", None)
        if not callable(iterator):
            closer = getattr(upstream, "close", None)
            if callable(closer):
                closer()
            self.fail(device_id)
            return False
        raw_iter = iterator(MJPEG_CHUNK_SIZE)
        first = b""
        try:
            for piece in raw_iter:
                if piece:
                    first = bytes(piece)
                    break
        except Exception:
            closer = getattr(upstream, "close", None)
            if callable(closer):
                closer()
            self.fail(device_id)
            return False
        if not first:
            closer = getattr(upstream, "close", None)
            if callable(closer):
                closer()
            self.fail(device_id)
            return False

        def _chunks():
            yield first
            yield from raw_iter

        primed = PrimedMjpeg(upstream, _chunks())
        if primed.device_id is None:
            primed.device_id = device_id
        self.store(device_id, primed)
        logger.info("mjpeg_prime_ready device=%s first_chunk=%s", device_id, len(first))
        return True


def start_stream_warmup(
    *,
    primer: MjpegPrimeCache,
    background: Callable[[Callable[[], None]], None],
    farm: Any,
    platform: Any,
    slot_id: int,
    device_id: str,
    platform_username: str,
    platform_password: str,
    after: Callable[[], None] | None = None,
) -> None:
    """Wake + prime immediately; run slow Farm prepare/lockdown after first JPEG."""
    primer.begin(device_id)

    def work() -> None:
        try:
            request_device_stream_wake(farm, int(slot_id))
            primer.prime_from_platform(
                platform,
                device_id=device_id,
                platform_username=platform_username,
                platform_password=platform_password,
            )
        except Exception:
            logger.warning("stream_warmup_failed slot=%s", slot_id)
            primer.fail(device_id)
        if after is None:
            return
        try:
            after()
        except Exception:
            logger.warning("stream_warmup_after_failed slot=%s", slot_id)

    background(work)
