// Run with: node --test verifier/
import assert from "node:assert/strict";
import fs from "node:fs";
import crypto from "node:crypto";
import test from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { shake128, shake256, sha3_256 } from "./keccak.js";
import { verify } from "./mldsa.js";
import { verifyReceiptOffline, verifyReceiptOnChain, verifyReceiptOnline } from "./receipt.js";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (p) => JSON.parse(fs.readFileSync(path.join(root, p), "utf8"));
const h = (s) => Uint8Array.from(Buffer.from(s, "hex"));
const dir = "docs/examples/tap-receipt/";
const failed = (checks) => checks.filter((c) => !c.ok).map((c) => c.name);

test("keccak matches node's built-in SHA-3 family", () => {
  for (const len of [0, 1, 135, 136, 137, 400]) {
    const data = Buffer.alloc(len, 7);
    assert.deepEqual(Buffer.from(shake128(data, 400)), crypto.createHash("shake128", { outputLength: 400 }).update(data).digest());
    assert.deepEqual(Buffer.from(shake256(data, 200)), crypto.createHash("shake256", { outputLength: 200 }).update(data).digest());
    assert.deepEqual(Buffer.from(sha3_256(data)), crypto.createHash("sha3-256").update(data).digest());
  }
});

test("ML-DSA-65 verifies the near-crypto vectors and rejects altered input", () => {
  for (const c of load("tests/vectors/mldsa65.json").cases) {
    const [pk, msg] = [h(c.public_key), h(c.message)];
    assert.equal(verify(pk, h(c.signature_deterministic), msg), true);
    assert.equal(verify(pk, h(c.signature_reference), msg), true);
    const badMessage = Uint8Array.from(msg);
    badMessage[0] ^= 1;
    assert.equal(verify(pk, h(c.signature_reference), badMessage), false);
    const badSig = h(c.signature_reference);
    badSig[100] ^= 1;
    assert.equal(verify(pk, badSig, msg), false);
    assert.equal(verify(pk, badSig.subarray(1), msg), false);
  }
});

test("the real testnet receipt passes every offline and on-chain check", async () => {
  const receipt = load(dir + "receipt.json");
  const outcome = load(dir + "outcome.json");
  const keys = load(dir + "owner-keys.json");
  const checks = [...(await verifyReceiptOffline(receipt)), ...verifyReceiptOnChain(receipt, outcome, keys)];
  assert.equal(checks.length, 20);
  assert.deepEqual(failed(checks), []);
});

test("each kind of tampering is caught", async () => {
  const receipt = load(dir + "receipt.json");
  const change = (fn) => {
    const copy = structuredClone(receipt);
    fn(copy);
    return copy;
  };
  const cases = [
    ["request amount changed", (r) => (r.request.asset_amount = "1"), "request hash recomputes"],
    ["cheque payee changed", (r) => (r.cheque.to = "eve.testnet"), "cheque is exactly what the request asks for"],
    ["key id swapped", (r) => (r.approval.key_id = "00".repeat(32)), "agent key id is the hash of the public key"],
    ["passport limit raised", (r) => (r.passport.body.limits.per_payment_max = "9".repeat(30)), "owner signature (ML-DSA-65) valid"],
    ["passport hash replaced", (r) => (r.passport.hash = "00".repeat(32)), "passport hash matches the receipt"],
    [
      "buyer signature replaced",
      (r) => (r.approval.signature = Buffer.alloc(3309).toString("base64")),
      "buyer's ML-DSA-65 signature covers the cheque",
    ],
  ];
  for (const [label, mutate, expected] of cases) {
    assert.ok(failed(await verifyReceiptOffline(change(mutate))).includes(expected), label);
  }
  assert.ok(failed(await verifyReceiptOffline({})).length > 0);
  assert.ok(failed(await verifyReceiptOffline({ request: 1 })).length > 0);
});

test("a wrong on-chain outcome is reported", () => {
  const receipt = load(dir + "receipt.json");
  const outcome = load(dir + "outcome.json");
  const other = structuredClone(outcome);
  other.transaction.hash = "OTHER";
  assert.ok(failed(verifyReceiptOnChain(receipt, other)).includes("transaction hash matches"));
  const noEvents = structuredClone(outcome);
  for (const r of noEvents.receipts_outcome) r.outcome.logs = [];
  assert.ok(failed(verifyReceiptOnChain(receipt, noEvents)).includes("vault emitted payment_authorized for this cheque"));
  assert.ok(failed(verifyReceiptOnChain(receipt, outcome, ["ml-dsa-65-hash:nope"])).includes("owner key is on the owner account on-chain"));
});

test("the online check calls the RPC for the transaction and the owner's keys", async () => {
  const receipt = load(dir + "receipt.json");
  const outcome = load(dir + "outcome.json");
  const keys = load(dir + "owner-keys.json");
  const calls = [];
  const fakeFetch = async (url, init) => {
    const request = JSON.parse(init.body);
    calls.push([url, request.method, request.params]);
    const result = request.method === "tx" ? outcome : { keys: keys.map((k) => ({ public_key: k })) };
    return { json: async () => ({ result }) };
  };
  const checks = await verifyReceiptOnline(receipt, { fetchImpl: fakeFetch });
  assert.deepEqual(failed(checks), []);
  assert.equal(calls[0][0], "https://rpc.testnet.near.org");
  assert.equal(calls[0][2].sender_account_id, receipt.settlement.relayer);
  assert.equal(calls[1][2].account_id, receipt.passport.body.owner);
  const broken = await verifyReceiptOnline(receipt, { fetchImpl: async () => { throw new Error("offline"); } });
  assert.equal(broken[0].ok, false);
});
