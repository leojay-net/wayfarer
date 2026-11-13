// Keccak-f[1600] sponge: SHAKE128, SHAKE256 and SHA3-256, written for this project from FIPS 202.
// BigInt lanes keep it short and easy to audit; speed is fine for one verification.

const MASK = (1n << 64n) - 1n;
const rotl = (x, n) => (n === 0 ? x : ((x << BigInt(n)) | (x >> BigInt(64 - n))) & MASK);

const ROUND_CONSTANTS = (() => {
  const out = [];
  let r = 1;
  for (let round = 0; round < 24; round++) {
    let rc = 0n;
    for (let j = 0; j < 7; j++) {
      r = ((r << 1) ^ ((r >> 7) * 0x71)) & 0xff;
      if (r & 2) rc ^= 1n << BigInt((1 << j) - 1);
    }
    out.push(rc);
  }
  return out;
})();

const ROTATIONS = (() => {
  const table = new Array(25).fill(0);
  let x = 1;
  let y = 0;
  for (let t = 0; t < 24; t++) {
    table[x + 5 * y] = (((t + 1) * (t + 2)) / 2) % 64;
    [x, y] = [y, (2 * x + 3 * y) % 5];
  }
  return table;
})();

function permute(a) {
  const c = new Array(5);
  const b = new Array(25);
  for (let round = 0; round < 24; round++) {
    for (let x = 0; x < 5; x++) c[x] = a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20];
    for (let x = 0; x < 5; x++) {
      const d = c[(x + 4) % 5] ^ rotl(c[(x + 1) % 5], 1);
      for (let y = 0; y < 25; y += 5) a[x + y] ^= d;
    }
    for (let x = 0; x < 5; x++) {
      for (let y = 0; y < 5; y++) b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(a[x + 5 * y], ROTATIONS[x + 5 * y]);
    }
    for (let y = 0; y < 25; y += 5) {
      for (let x = 0; x < 5; x++) a[x + y] = b[x + y] ^ (~b[((x + 1) % 5) + y] & MASK & b[((x + 2) % 5) + y]);
    }
    a[0] ^= ROUND_CONSTANTS[round];
  }
}

function sponge(rate, suffix, data, outLength) {
  const state = new Array(25).fill(0n);
  const absorb = (block) => {
    for (let i = 0; i < block.length; i += 8) {
      let lane = 0n;
      for (let k = 7; k >= 0; k--) lane = (lane << 8n) | BigInt(block[i + k]);
      state[i / 8] ^= lane;
    }
    permute(state);
  };
  let offset = 0;
  for (; offset + rate <= data.length; offset += rate) absorb(data.subarray(offset, offset + rate));
  const last = new Uint8Array(rate);
  last.set(data.subarray(offset));
  last[data.length - offset] ^= suffix;
  last[rate - 1] ^= 0x80;
  absorb(last);
  const out = new Uint8Array(outLength);
  let produced = 0;
  for (;;) {
    for (let i = 0; i < rate && produced < outLength; i++) {
      out[produced++] = Number((state[i >> 3] >> BigInt(8 * (i & 7))) & 0xffn);
    }
    if (produced >= outLength) return out;
    permute(state);
  }
}

export const shake128 = (data, length) => sponge(168, 0x1f, data, length);
export const shake256 = (data, length) => sponge(136, 0x1f, data, length);
export const sha3_256 = (data) => sponge(136, 0x06, data, 32);
