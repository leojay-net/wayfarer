"""Canonical binary encoding shared by every tap message (borsh-compatible primitives, strict decoding).

Strings are u32-length-prefixed UTF-8, integers are little-endian, fixed byte arrays have no prefix. The decoder rejects
short input, oversize strings and trailing bytes, so a message has exactly one valid encoding.
"""

from __future__ import annotations

import struct

from wayfarer.errors import TapError

MAX_STRING = 256


class Writer:
    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def raw(self, data: bytes) -> Writer:
        self._parts.append(data)
        return self

    def u8(self, value: int) -> Writer:
        return self.raw(struct.pack("<B", value))

    def u32(self, value: int) -> Writer:
        return self.raw(struct.pack("<I", value))

    def u64(self, value: int) -> Writer:
        return self.raw(struct.pack("<Q", value))

    def u128(self, value: int) -> Writer:
        return self.raw(value.to_bytes(16, "little"))

    def string(self, value: str) -> Writer:
        encoded = value.encode()
        if len(encoded) > MAX_STRING:
            raise TapError("A text field is too long.")
        return self.u32(len(encoded)).raw(encoded)

    def getvalue(self) -> bytes:
        return b"".join(self._parts)


class Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def raw(self, count: int) -> bytes:
        if count < 0 or self._pos + count > len(self._data):
            raise TapError("The message is truncated.")
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def u8(self) -> int:
        return int(struct.unpack("<B", self.raw(1))[0])

    def u32(self) -> int:
        return int(struct.unpack("<I", self.raw(4))[0])

    def u64(self) -> int:
        return int(struct.unpack("<Q", self.raw(8))[0])

    def u128(self) -> int:
        return int.from_bytes(self.raw(16), "little")

    def string(self) -> str:
        length = self.u32()
        if length > MAX_STRING:
            raise TapError("A text field is too long.")
        try:
            return self.raw(length).decode()
        except UnicodeDecodeError as error:
            raise TapError("A text field is not valid UTF-8.") from error

    def finish(self) -> None:
        if self._pos != len(self._data):
            raise TapError("The message has unexpected trailing bytes.")
