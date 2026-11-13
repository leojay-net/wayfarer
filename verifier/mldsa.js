// ML-DSA-65 (FIPS 204) signature verification only, written from the standard for this project.
// Checked against the vectors in tests/vectors/mldsa65.json (made by nearcore's own near-crypto). Not audited.
import { shake128, shake256 } from "./keccak.js";

const Q = 8380417;
const N = 256;
const D = 13;
const TAU = 49;
const GAMMA1 = 1 << 19;
const GAMMA2 = (Q - 1) / 32;
const K = 6;
const L = 5;
const BETA = TAU * 4;
const OMEGA = 55;
export const PUBLIC_KEY_SIZE = 1952;
export const SIGNATURE_SIZE = 3309;
const C_TILDE = 48;

const mod = (x) => ((x % Q) + Q) % Q;
const mulmod = (a, b) => (a * b) % Q; // a, b < Q so the product stays below 2^53

function powmod(base, exp) {
  let result = 1;
  let b = base % Q;
  for (let e = exp; e > 0; e = Math.floor(e / 2)) {
    if (e % 2 === 1) result = mulmod(result, b);
    b = mulmod(b, b);
  }
  return result;
}

const bitReverse8 = (x) => parseInt(x.toString(2).padStart(8, "0").split("").reverse().join(""), 2);
const ZETAS = Array.from({ length: N }, (_, i) => powmod(1753, bitReverse8(i)));
const INV_N = powmod(N, Q - 2);

function ntt(poly) {
  const w = poly.slice();
  let m = 0;
  for (let length = 128; length >= 1; length >>= 1) {
    for (let start = 0; start < N; start += 2 * length) {
      const z = ZETAS[++m];
      for (let j = start; j < start + length; j++) {
        const t = mulmod(z, w[j + length]);
        w[j + length] = (w[j] - t + Q) % Q;
        w[j] = (w[j] + t) % Q;
      }
    }
  }
  return w;
}

function invNtt(poly) {
  const w = poly.slice();
  let m = N;
  for (let length = 1; length < N; length *= 2) {
    for (let start = 0; start < N; start += 2 * length) {
      const z = Q - ZETAS[--m]; // -zeta mod Q
      for (let j = start; j < start + length; j++) {
        const t = w[j];
        w[j] = (t + w[j + length]) % Q;
        w[j + length] = mulmod(z, (t - w[j + length] + Q) % Q);
      }
    }
  }
  return w.map((x) => mulmod(x, INV_N));
}

function unpackBits(data, offset, bits, count) {
  const out = new Array(count);
  const mask = (1 << bits) - 1;
  let acc = 0;
  let have = 0;
  let pos = offset;
  for (let i = 0; i < count; i++) {
    while (have < bits) {
      acc |= data[pos++] << have;
      have += 8;
    }
    out[i] = acc & mask;
    acc >>>= bits;
    have -= bits;
  }
  return out;
}

function expandA(rho) {
  const matrix = [];
  for (let r = 0; r < K; r++) {
    const row = [];
    for (let s = 0; s < L; s++) {
      const seed = new Uint8Array(34);
      seed.set(rho);
      seed[32] = s;
      seed[33] = r;
      let length = 840;
      let coeffs;
      for (;;) {
        const buf = shake128(seed, length);
        coeffs = [];
        for (let i = 0; i + 2 < buf.length && coeffs.length < N; i += 3) {
          const v = buf[i] | (buf[i + 1] << 8) | ((buf[i + 2] & 0x7f) << 16);
          if (v < Q) coeffs.push(v);
        }
        if (coeffs.length === N) break;
        length *= 2;
      }
      row.push(coeffs);
    }
    matrix.push(row);
  }
  return matrix;
}

function sampleInBall(seed) {
  const stream = shake256(seed, 8 + 1024);
  let signs = 0n;
  for (let i = 7; i >= 0; i--) signs = (signs << 8n) | BigInt(stream[i]);
  let pos = 8;
  const c = new Array(N).fill(0);
  for (let i = N - TAU; i < N; i++) {
    let j;
    do {
      j = stream[pos++];
    } while (j > i);
    c[i] = c[j];
    c[j] = 1 - 2 * Number(signs & 1n);
    signs >>= 1n;
  }
  return c.map(mod);
}

function centered(x, modulus) {
  const r = ((x % modulus) + modulus) % modulus;
  return r > Math.floor(modulus / 2) ? r - modulus : r;
}

function decompose(r) {
  const rr = mod(r);
  const r0 = centered(rr, 2 * GAMMA2);
  if (rr - r0 === Q - 1) return [0, r0 - 1];
  return [(rr - r0) / (2 * GAMMA2), r0];
}

function useHint(h, r) {
  const m = (Q - 1) / (2 * GAMMA2);
  const [r1, r0] = decompose(r);
  if (h === 1) return r0 > 0 ? (r1 + 1) % m : (r1 - 1 + m) % m;
  return r1;
}

function hintUnpack(data) {
  const h = Array.from({ length: K }, () => new Array(N).fill(0));
  let index = 0;
  for (let i = 0; i < K; i++) {
    const limit = data[OMEGA + i];
    if (limit < index || limit > OMEGA) return null;
    const first = index;
    while (index < limit) {
      if (index > first && data[index - 1] >= data[index]) return null;
      h[i][data[index]] = 1;
      index++;
    }
  }
  for (let i = index; i < OMEGA; i++) if (data[i] !== 0) return null;
  return h;
}

function w1Encode(w1) {
  const out = new Uint8Array((K * N) / 2);
  let pos = 0;
  for (const poly of w1) for (let i = 0; i < N; i += 2) out[pos++] = poly[i] | (poly[i + 1] << 4);
  return out;
}

const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
};

/** Verify a pure ML-DSA-65 signature with the empty context. Malformed input returns false. */
export function verify(publicKey, signature, message) {
  if (publicKey.length !== PUBLIC_KEY_SIZE || signature.length !== SIGNATURE_SIZE) return false;
  const rho = publicKey.subarray(0, 32);
  const t1 = Array.from({ length: K }, (_, i) => unpackBits(publicKey, 32 + 320 * i, 10, N));
  const cTilde = signature.subarray(0, C_TILDE);
  const z = Array.from({ length: L }, (_, i) =>
    unpackBits(signature, C_TILDE + 640 * i, 20, N).map((v) => GAMMA1 - v),
  );
  const hints = hintUnpack(signature.subarray(C_TILDE + 640 * L));
  if (hints === null) return false;
  if (z.some((poly) => poly.some((c) => Math.abs(c) >= GAMMA1 - BETA))) return false;
  const aHat = expandA(rho);
  const tr = shake256(publicKey, 64);
  const mu = shake256(concat(tr, new Uint8Array([0, 0]), message), 64);
  const cHat = ntt(sampleInBall(cTilde));
  const zHat = z.map((poly) => ntt(poly.map(mod)));
  const w1 = [];
  for (let i = 0; i < K; i++) {
    const t1Hat = ntt(t1[i].map((c) => mulmod(c, 1 << D)));
    const acc = new Array(N).fill(0);
    for (let j = 0; j < L; j++) for (let n = 0; n < N; n++) acc[n] = (acc[n] + mulmod(aHat[i][j][n], zHat[j][n])) % Q;
    const approx = invNtt(acc.map((x, n) => (x - mulmod(cHat[n], t1Hat[n]) + Q) % Q));
    w1.push(approx.map((r, n) => useHint(hints[i][n], r)));
  }
  const expected = shake256(concat(mu, w1Encode(w1)), C_TILDE);
  return expected.every((b, i) => b === cTilde[i]);
}
