"""Ed25519 signatures (RFC 8032), pure Python.

PROTOTYPE: this is the reference algorithm. It is checked against the RFC test vector and OpenSSL, but it is
NOT constant-time and not hardened. Use it for throwaway testnet keys and for verifying public signatures only.
"""

from __future__ import annotations

import hashlib

Point = tuple[int, int, int, int]

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int:
    x_squared = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x_squared, (_P + 3) // 8, _P)
    if (x * x - x_squared) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x_squared) % _P or (x == 0 and sign):
        raise ValueError("point is not on the curve")
    return _P - x if x & 1 != sign else x


_BASE_Y = 4 * pow(5, _P - 2, _P) % _P
_BASE_X = _recover_x(_BASE_Y, 0)
_BASE: Point = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _P)
_IDENTITY: Point = (0, 1, 1, 0)


def _add(p: Point, q: Point) -> Point:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar: int, point: Point) -> Point:
    result = _IDENTITY
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encode(point: Point) -> bytes:
    z_inv = pow(point[2], _P - 2, _P)
    x, y = point[0] * z_inv % _P, point[1] * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes) -> Point:
    if len(data) != 32:
        raise ValueError("a point is 32 bytes")
    value = int.from_bytes(data, "little")
    y, sign = value & ((1 << 255) - 1), value >> 255
    if y >= _P:
        raise ValueError("non-canonical point")
    x = _recover_x(y, sign)
    return (x, y, 1, x * y % _P)


def _hash_to_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _expand_seed(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte public key for a 32-byte seed."""
    return _encode(_multiply(_expand_seed(seed)[0], _BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    """Return the 64-byte signature of ``message``."""
    scalar, prefix = _expand_seed(seed)
    public = _encode(_multiply(scalar, _BASE))
    r = _hash_to_int(prefix + message) % _L
    r_point = _encode(_multiply(r, _BASE))
    k = _hash_to_int(r_point + public + message) % _L
    return r_point + ((r + k * scalar) % _L).to_bytes(32, "little")


def verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """Return True only if ``signature`` is a valid signature of ``message`` by ``public_key``."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        a_point = _decode(public_key)
        r_point = _decode(signature[:32])
    except ValueError:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = _hash_to_int(signature[:32] + public_key + message) % _L
    left = _multiply(s, _BASE)
    right = _add(r_point, _multiply(k, a_point))
    return _encode(left) == _encode(right)
