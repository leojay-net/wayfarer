"""ML-DSA-65 (FIPS 204) in pure Python: key generation from a seed, signing and verification.

Written from the FIPS 204 text (Algorithms 6, 7, 8, 24-37, 41, 42 and the helper functions) and cross-checked against
nearcore's own ``near-crypto`` (``tools/pqref``): public keys derived from a seed are byte-identical, and signatures
made here verify there and vice versa (``tests/vectors/mldsa65.json``, ``scripts/crosscheck_mldsa.py``).

Not constant-time and not hardened against side channels: this is a reference signer for testnet keys and tests.
Do not use it to protect real value. Signing takes on the order of a second in CPython.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import NamedTuple

from wayfarer.errors import WayfarerError

# ML-DSA-65 parameters (FIPS 204, Table 1)
Q = 8380417
N = 256
D = 13
TAU = 49
LAMBDA = 192
GAMMA1 = 1 << 19
GAMMA2 = (Q - 1) // 32
K = 6
L = 5
ETA = 4
BETA = TAU * ETA
OMEGA = 55

SEED_SIZE = 32
PUBLIC_KEY_SIZE = 1952
SECRET_KEY_SIZE = 4032
SIGNATURE_SIZE = 3309
_C_TILDE = LAMBDA // 4  # 48 bytes

Poly = list[int]


class MlDsaError(WayfarerError):
    """Raised for malformed ML-DSA inputs (never for an ordinary verification failure)."""


def _brv8(x: int) -> int:
    return int(f"{x:08b}"[::-1], 2)


_ZETAS = [pow(1753, _brv8(i), Q) for i in range(N)]
_INV_N = pow(N, -1, Q)  # 8347681


# ---------------------------------------------------------------------------------------------- NTT


def _ntt(poly: Poly) -> Poly:
    w = list(poly)
    m = 0
    length = 128
    while length >= 1:
        for start in range(0, N, 2 * length):
            m += 1
            z = _ZETAS[m]
            for j in range(start, start + length):
                t = z * w[j + length] % Q
                w[j + length] = (w[j] - t) % Q
                w[j] = (w[j] + t) % Q
        length //= 2
    return w


def _inv_ntt(poly: Poly) -> Poly:
    w = list(poly)
    m = N
    length = 1
    while length < N:
        for start in range(0, N, 2 * length):
            m -= 1
            z = -_ZETAS[m]
            for j in range(start, start + length):
                t = w[j]
                w[j] = (t + w[j + length]) % Q
                w[j + length] = z * (t - w[j + length]) % Q
        length *= 2
    return [x * _INV_N % Q for x in w]


def _mul(a: Poly, b: Poly) -> Poly:
    return [x * y % Q for x, y in zip(a, b, strict=True)]


def _add(a: Poly, b: Poly) -> Poly:
    return [(x + y) % Q for x, y in zip(a, b, strict=True)]


def _sub(a: Poly, b: Poly) -> Poly:
    return [(x - y) % Q for x, y in zip(a, b, strict=True)]


def _dot(row: list[Poly], vec: list[Poly]) -> Poly:
    acc = [0] * N
    for a, b in zip(row, vec, strict=True):
        acc = [(s + x * y) % Q for s, x, y in zip(acc, a, b, strict=True)]
    return acc


# ------------------------------------------------------------------------------------ rounding / hints


def _centered(x: int, modulus: int = Q) -> int:
    """``x mod± modulus``: the representative in ``(-modulus/2, modulus/2]``."""
    x %= modulus
    return x - modulus if x > modulus // 2 else x


def _power2round(r: int) -> tuple[int, int]:
    r %= Q
    r0 = _centered(r, 1 << D)
    return (r - r0) >> D, r0


def _decompose(r: int) -> tuple[int, int]:
    r %= Q
    r0 = _centered(r, 2 * GAMMA2)
    if r - r0 == Q - 1:
        return 0, r0 - 1
    return (r - r0) // (2 * GAMMA2), r0


def _high(r: int) -> int:
    return _decompose(r)[0]


def _use_hint(h: int, r: int) -> int:
    m = (Q - 1) // (2 * GAMMA2)
    r1, r0 = _decompose(r)
    if h == 1:
        return (r1 + 1) % m if r0 > 0 else (r1 - 1) % m
    return r1


def _inf_norm(vec: list[Poly]) -> int:
    return max(abs(_centered(c)) for poly in vec for c in poly)


# ------------------------------------------------------------------------------------------- packing


def _pack_bits(values: list[int], bits: int) -> bytes:
    acc = 0
    for i, v in enumerate(values):
        acc |= v << (i * bits)
    return acc.to_bytes(len(values) * bits // 8, "little")


def _unpack_bits(data: bytes, bits: int, count: int) -> list[int]:
    acc = int.from_bytes(data, "little")
    mask = (1 << bits) - 1
    return [(acc >> (i * bits)) & mask for i in range(count)]


def _pack_pm(poly: Poly, a: int, b: int) -> bytes:
    """BitPack: coefficients in ``[-a, b]`` stored as ``b - w``."""
    return _pack_bits([b - _centered(c) for c in poly], (a + b).bit_length())


def _unpack_pm(data: bytes, a: int, b: int) -> Poly:
    return [(b - v) % Q for v in _unpack_bits(data, (a + b).bit_length(), N)]


def _hint_pack(h: list[list[int]]) -> bytes:
    out = bytearray(OMEGA + K)
    index = 0
    for i in range(K):
        for j in range(N):
            if h[i][j]:
                out[index] = j
                index += 1
        out[OMEGA + i] = index
    return bytes(out)


def _hint_unpack(data: bytes) -> list[list[int]] | None:
    """Return the hint vector, or ``None`` if the encoding is malformed (FIPS 204 Algorithm 21)."""
    h = [[0] * N for _ in range(K)]
    index = 0
    for i in range(K):
        limit = data[OMEGA + i]
        if limit < index or limit > OMEGA:
            return None
        first = index
        while index < limit:
            if index > first and data[index - 1] >= data[index]:
                return None
            h[i][data[index]] = 1
            index += 1
    if any(data[index:OMEGA]):
        return None
    return h


# -------------------------------------------------------------------------------------------- sampling


def _shake128(data: bytes, length: int) -> bytes:
    return hashlib.shake_128(data).digest(length)


def _shake256(data: bytes, length: int) -> bytes:
    return hashlib.shake_256(data).digest(length)


def _expand_a(rho: bytes) -> list[list[Poly]]:
    """Matrix A already in the NTT domain (FIPS 204 Algorithm 32)."""
    matrix: list[list[Poly]] = []
    for r in range(K):
        row: list[Poly] = []
        for s in range(L):
            stream = hashlib.shake_128(rho + bytes([s, r]))
            length = 840
            while True:
                buf = stream.digest(length)
                coeffs: Poly = []
                for i in range(0, len(buf) - 2, 3):
                    v = buf[i] | (buf[i + 1] << 8) | ((buf[i + 2] & 0x7F) << 16)
                    if v < Q:
                        coeffs.append(v)
                        if len(coeffs) == N:
                            break
                if len(coeffs) == N:
                    break
                length *= 2
            row.append(coeffs)
        matrix.append(row)
    return matrix


def _expand_s(rho_prime: bytes) -> tuple[list[Poly], list[Poly]]:
    def sample(r: int) -> Poly:
        length = 272
        while True:
            buf = _shake256(rho_prime + r.to_bytes(2, "little"), length)
            coeffs: Poly = []
            for byte in buf:
                for nibble in (byte & 15, byte >> 4):
                    if nibble < 9:
                        coeffs.append((ETA - nibble) % Q)
                        if len(coeffs) == N:
                            return coeffs
            length *= 2

    s1 = [sample(r) for r in range(L)]
    s2 = [sample(L + r) for r in range(K)]
    return s1, s2


def _expand_mask(rho2: bytes, kappa: int) -> list[Poly]:
    out = []
    for r in range(L):
        buf = _shake256(rho2 + (kappa + r).to_bytes(2, "little"), 640)
        out.append(_unpack_pm(buf, GAMMA1 - 1, GAMMA1))
    return out


def _sample_in_ball(seed: bytes) -> Poly:
    stream = hashlib.shake_256(seed).digest(8 + 1024)
    signs = int.from_bytes(stream[:8], "little")
    pos = 8
    c = [0] * N
    for i in range(N - TAU, N):
        while True:
            j = stream[pos]
            pos += 1
            if j <= i:
                break
        c[i] = c[j]
        c[j] = 1 - 2 * (signs & 1)
        signs >>= 1
    return [x % Q for x in c]


# ---------------------------------------------------------------------------------------------- keys


class _Expanded(NamedTuple):
    public_key: bytes
    a_hat: list[list[Poly]]
    k_seed: bytes
    tr: bytes
    s1_hat: list[Poly]
    s2_hat: list[Poly]
    t0_hat: list[Poly]


@lru_cache(maxsize=8)
def _expand_seed(seed: bytes) -> _Expanded:
    """FIPS 204 Algorithm 6 (ML-DSA.KeyGen_internal) from the 32-byte seed ``xi``."""
    expanded = _shake256(seed + bytes([K, L]), 128)
    rho, rho_prime, k_seed = expanded[:32], expanded[32:96], expanded[96:]
    a_hat = _expand_a(rho)
    s1, s2 = _expand_s(rho_prime)
    s1_hat = [_ntt(p) for p in s1]
    t = [_add(_inv_ntt(_dot(a_hat[i], s1_hat)), s2[i]) for i in range(K)]
    t1: list[Poly] = []
    t0: list[Poly] = []
    for poly in t:
        pairs = [_power2round(c) for c in poly]
        t1.append([p[0] for p in pairs])
        t0.append([p[1] % Q for p in pairs])
    public_key = rho + b"".join(_pack_bits(p, 10) for p in t1)
    return _Expanded(
        public_key=public_key,
        a_hat=a_hat,
        k_seed=k_seed,
        tr=_shake256(public_key, 64),
        s1_hat=s1_hat,
        s2_hat=[_ntt(p) for p in s2],
        t0_hat=[_ntt(p) for p in t0],
    )


def public_key_from_seed(seed: bytes) -> bytes:
    """The 1,952-byte ML-DSA-65 public key for a 32-byte seed."""
    _check_seed(seed)
    return _expand_seed(seed).public_key


def _check_seed(seed: bytes) -> None:
    if len(seed) != SEED_SIZE:
        raise MlDsaError(f"ML-DSA seed must be {SEED_SIZE} bytes.")


def _message_prime(message: bytes, context: bytes) -> bytes:
    if len(context) > 255:
        raise MlDsaError("ML-DSA context must be at most 255 bytes.")
    return bytes([0, len(context)]) + context + message


def _w1_encode(w1: list[Poly]) -> bytes:
    return b"".join(_pack_bits(p, 4) for p in w1)


# ------------------------------------------------------------------------------------------ sign / verify


def sign(seed: bytes, message: bytes, *, context: bytes = b"", randomness: bytes | None = None) -> bytes:
    """Sign ``message`` (pure ML-DSA, FIPS 204 Algorithm 2) and return the 3,309-byte signature.

    ``randomness`` is the 32-byte ``rnd`` input. ``None`` draws fresh randomness (the hedged variant FIPS 204
    recommends); pass ``bytes(32)`` for the deterministic variant used in tests.
    """
    _check_seed(seed)
    rnd = os.urandom(32) if randomness is None else randomness
    if len(rnd) != 32:
        raise MlDsaError("ML-DSA randomness must be 32 bytes.")
    key = _expand_seed(seed)
    mu = _shake256(key.tr + _message_prime(message, context), 64)
    rho2 = _shake256(key.k_seed + rnd + mu, 64)
    kappa = 0
    while True:
        y = _expand_mask(rho2, kappa)
        kappa += L
        y_hat = [_ntt(p) for p in y]
        w = [_inv_ntt(_dot(key.a_hat[i], y_hat)) for i in range(K)]
        w1 = [[_high(c) for c in poly] for poly in w]
        c_tilde = _shake256(mu + _w1_encode(w1), _C_TILDE)
        c_hat = _ntt(_sample_in_ball(c_tilde))
        cs1 = [_inv_ntt(_mul(c_hat, p)) for p in key.s1_hat]
        cs2 = [_inv_ntt(_mul(c_hat, p)) for p in key.s2_hat]
        z = [_add(y[i], cs1[i]) for i in range(L)]
        w_cs2 = [_sub(w[i], cs2[i]) for i in range(K)]
        r0 = [[_decompose(c)[1] for c in poly] for poly in w_cs2]
        if _inf_norm(z) >= GAMMA1 - BETA or max(abs(c) for poly in r0 for c in poly) >= GAMMA2 - BETA:
            continue
        ct0 = [_inv_ntt(_mul(c_hat, p)) for p in key.t0_hat]
        if _inf_norm(ct0) >= GAMMA2:
            continue
        hints = [[int(_high((a + b) % Q) != _high(a)) for a, b in zip(w_cs2[i], ct0[i], strict=True)] for i in range(K)]
        if sum(map(sum, hints)) > OMEGA:
            continue
        z_bytes = b"".join(_pack_pm(p, GAMMA1 - 1, GAMMA1) for p in z)
        signature = c_tilde + z_bytes + _hint_pack(hints)
        if len(signature) != SIGNATURE_SIZE:  # pragma: no cover - guards the packing arithmetic
            raise MlDsaError("Internal error: wrong signature size.")
        return signature


def verify(public_key: bytes, signature: bytes, message: bytes, *, context: bytes = b"") -> bool:
    """Verify a pure ML-DSA-65 signature (FIPS 204 Algorithm 3). Malformed input returns ``False``."""
    if len(public_key) != PUBLIC_KEY_SIZE or len(signature) != SIGNATURE_SIZE or len(context) > 255:
        return False
    rho = public_key[:32]
    t1 = [_unpack_bits(public_key[32 + 320 * i : 32 + 320 * (i + 1)], 10, N) for i in range(K)]
    c_tilde = signature[:_C_TILDE]
    z_len = 640
    z = [_unpack_pm(signature[_C_TILDE + z_len * i : _C_TILDE + z_len * (i + 1)], GAMMA1 - 1, GAMMA1) for i in range(L)]
    hints = _hint_unpack(signature[_C_TILDE + z_len * L :])
    if hints is None or _inf_norm(z) >= GAMMA1 - BETA:
        return False
    a_hat = _expand_a(rho)
    tr = _shake256(public_key, 64)
    mu = _shake256(tr + _message_prime(message, context), 64)
    c_hat = _ntt(_sample_in_ball(c_tilde))
    z_hat = [_ntt(p) for p in z]
    w1: list[Poly] = []
    for i in range(K):
        t1_hat = _ntt([(c << D) % Q for c in t1[i]])
        w_approx = _inv_ntt(_sub(_dot(a_hat[i], z_hat), _mul(c_hat, t1_hat)))
        w1.append([_use_hint(h, r) for h, r in zip(hints[i], w_approx, strict=True)])
    return _shake256(mu + _w1_encode(w1), _C_TILDE) == c_tilde
