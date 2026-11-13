"""Base58 (Bitcoin alphabet), as used by NEAR for keys and hashes."""

from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    """Encode bytes; leading zero bytes become leading ``1`` characters."""
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _ALPHABET[remainder] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    """Decode a base58 string; raises ``ValueError`` on characters outside the alphabet."""
    number = 0
    for char in text:
        index = _ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"invalid base58 character {char!r}")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body
