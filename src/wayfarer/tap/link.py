"""Moving bytes between two phones: a link interface, an in-memory pair for tests, and numbered text frames for QR.

Real transports (NFC card emulation, Nearby/Bluetooth) live in the Android app and implement the same ``send`` /
``receive`` contract. The text-frame format is the always-works fallback: every frame carries a session id, its index,
the frame count and a CRC-32, so frames may arrive in any order, twice, or damaged, and the assembler says what is missing.
"""

from __future__ import annotations

import base64
import binascii
import queue
from dataclasses import dataclass
from typing import Protocol

from wayfarer.errors import TapError

FRAME_PREFIX = "WF1"
DEFAULT_CHUNK_BYTES = 900


class PaymentLink(Protocol):
    def send(self, data: bytes) -> None: ...

    def receive(self, timeout_s: float = 5.0) -> bytes | None: ...


class LoopbackLink:
    """One end of an in-memory link. Use :meth:`pair` to get two connected ends."""

    def __init__(self, inbox: queue.Queue[bytes], outbox: queue.Queue[bytes]) -> None:
        self._inbox = inbox
        self._outbox = outbox

    @classmethod
    def pair(cls) -> tuple[LoopbackLink, LoopbackLink]:
        a_to_b: queue.Queue[bytes] = queue.Queue()
        b_to_a: queue.Queue[bytes] = queue.Queue()
        return cls(b_to_a, a_to_b), cls(a_to_b, b_to_a)

    def send(self, data: bytes) -> None:
        self._outbox.put(data)

    def receive(self, timeout_s: float = 5.0) -> bytes | None:
        try:
            return self._inbox.get(timeout=timeout_s)
        except queue.Empty:
            return None


def _crc(text: str) -> str:
    return f"{binascii.crc32(text.encode()) & 0xFFFFFFFF:08x}"


def encode_frames(payload: bytes, session: bytes, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> list[str]:
    """Split ``payload`` into text frames ``WF1.<session>.<index>.<count>.<base64url>.<crc>`` for an animated QR."""
    if len(session) != 4:
        raise TapError("The session id must be 4 bytes.")
    if chunk_bytes <= 0:
        raise TapError("The chunk size must be positive.")
    chunks = [payload[i : i + chunk_bytes] for i in range(0, len(payload), chunk_bytes)] or [b""]
    if len(chunks) > 999:
        raise TapError("The payload needs too many frames.")
    frames = []
    for index, chunk in enumerate(chunks):
        body = base64.urlsafe_b64encode(chunk).decode().rstrip("=")
        head = f"{FRAME_PREFIX}.{session.hex()}.{index}.{len(chunks)}.{body}"
        frames.append(f"{head}.{_crc(head)}")
    return frames


@dataclass(frozen=True, slots=True)
class Progress:
    received: int
    total: int
    missing: tuple[int, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


class FrameAssembler:
    """Collects frames of one session. Duplicates are harmless; damaged or foreign frames raise :class:`TapError`."""

    def __init__(self) -> None:
        self._session: str | None = None
        self._total: int | None = None
        self._chunks: dict[int, bytes] = {}

    def add(self, frame: str) -> Progress:
        parts = frame.strip().split(".")
        if len(parts) != 6 or parts[0] != FRAME_PREFIX:
            raise TapError("This is not a Wayfarer frame.")
        head = ".".join(parts[:5])
        if _crc(head) != parts[5]:
            raise TapError("A frame was damaged; scan it again.")
        session, index_text, total_text, body = parts[1], parts[2], parts[3], parts[4]
        if not (index_text.isdigit() and total_text.isdigit()):
            raise TapError("A frame header is invalid.")
        index, total = int(index_text), int(total_text)
        if total < 1 or index >= total:
            raise TapError("A frame header is invalid.")
        if self._session is None:
            self._session, self._total = session, total
        elif session != self._session or total != self._total:
            raise TapError("This frame belongs to a different transfer.")
        try:
            chunk = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except (binascii.Error, ValueError) as error:
            raise TapError("A frame body is invalid.") from error
        known = self._chunks.get(index)
        if known is not None and known != chunk:
            raise TapError("Two different frames claim the same position.")
        self._chunks[index] = chunk
        return self.progress()

    def progress(self) -> Progress:
        total = self._total or 0
        missing = tuple(i for i in range(total) if i not in self._chunks)
        return Progress(len(self._chunks), total, missing)

    def payload(self) -> bytes:
        if not self.progress().complete or self._total is None:
            raise TapError("Some frames are still missing.")
        return b"".join(self._chunks[i] for i in range(self._total))
