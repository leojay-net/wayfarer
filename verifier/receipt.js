// Approval receipt v2 checks in the browser. Mirrors src/wayfarer/proof.py and src/wayfarer/mandate.py: the check
// names are identical so the Python and JavaScript verifiers can be compared line by line.
import { sha3_256 } from "./keccak.js";
import { verify } from "./mldsa.js";

const RECEIPT_TYPE = "wayfarer.approval-receipt/v2";
const PASSPORT_TYPE = "wayfarer.mandate-passport/v1";
const enc = new TextEncoder();
const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
export const fromBase64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
export const sha256 = async (bytes) => new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));

function base58(bytes) {
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  let out = "";
  while (n > 0n) {
    out = ALPHABET[Number(n % 58n)] + out;
    n /= 58n;
  }
  for (const b of bytes) {
    if (b !== 0) break;
    out = "1" + out;
  }
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
const le = (value, bytes) => {
  const out = new Uint8Array(bytes);
  let v = BigInt(value);
  for (let i = 0; i < bytes; i++, v >>= 8n) out[i] = Number(v & 0xffn);
  return out;
};
const text = (s) => {
  const raw = enc.encode(s);
  return concat(le(raw.length, 4), raw);
};

/** Python's json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True). */
export function canonicalJson(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") {
    let out = '"';
    for (let i = 0; i < value.length; i++) {
      const c = value.charCodeAt(i);
      const ch = value[i];
      if (ch === '"') out += '\\"';
      else if (ch === "\\") out += "\\\\";
      else if (ch === "\n") out += "\\n";
      else if (ch === "\r") out += "\\r";
      else if (ch === "\t") out += "\\t";
      else if (ch === "\b") out += "\\b";
      else if (ch === "\f") out += "\\f";
      else if (c < 0x20 || c > 0x7e) out += "\\u" + c.toString(16).padStart(4, "0");
      else out += ch;
    }
    return out + '"';
  }
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  return (
    "{" +
    Object.keys(value)
      .sort()
      .map((k) => canonicalJson(k) + ":" + canonicalJson(value[k]))
      .join(",") +
    "}"
  );
}

export function requestMessage(r) {
  const hexBytes = Uint8Array.from(r.nonce.match(/../g) ?? [], (h) => parseInt(h, 16));
  return concat(
    enc.encode("wayfarer-request-v1"),
    text(r.merchant_account),
    text(r.merchant_name),
    text(r.fiat_currency),
    le(r.fiat_minor, 8),
    le(r.fiat_exponent, 1),
    text(r.token_id),
    le(r.asset_amount, 16),
    text(r.quote_id),
    text(r.rate),
    le(r.quote_expires_ms, 8),
    le(r.settle_by_ms, 8),
    hexBytes,
    le(r.created_ms, 8),
  );
}

export function chequeMessage(c) {
  return concat(
    enc.encode("wayfarer-cheque-v1"),
    text(c.vault),
    text(c.token_id),
    text(c.to),
    le(c.amount, 16),
    text(c.intent_id),
    le(c.expires_at_ms, 8),
    text(c.receipt_hash),
  );
}

const check = (name, ok, detail = "") => ({ name, ok: Boolean(ok), detail });

/** Everything that needs no network. Never throws on a bad receipt: a failed check is reported instead. */
export async function verifyReceiptOffline(receipt) {
  let request, approval, passport, claimed, vault, publicKey, signature;
  try {
    request = receipt.request;
    approval = receipt.approval;
    publicKey = fromBase64(approval.public_key);
    signature = fromBase64(approval.signature);
    passport = receipt.passport;
    claimed = receipt.cheque;
    vault = String(claimed.vault);
    requestMessage(request);
    canonicalJson(passport.body);
    fromBase64(passport.owner_public_key);
    fromBase64(passport.signature);
  } catch (error) {
    return [check("receipt is well formed", false, String(error))];
  }
  const requestHash = hex(await sha256(requestMessage(request)));
  const expected = {
    vault,
    token_id: request.token_id,
    to: request.merchant_account,
    amount: String(request.asset_amount),
    intent_id: "tap-" + request.nonce,
    expires_at_ms: request.settle_by_ms,
    receipt_hash: "wft:" + requestHash,
  };
  const keyId = String(approval.key_id);
  const body = passport.body ?? {};
  const limits = body.limits ?? {};
  const signedBytes = concat(enc.encode("wayfarer-mandate-passport-v1\0"), enc.encode(canonicalJson(body)));
  const ownerKey = fromBase64(passport.owner_public_key);
  const checks = [
    check("receipt is well formed", receipt.type === RECEIPT_TYPE),
    check("request hash recomputes", receipt.request_hash === requestHash),
    check("cheque is exactly what the request asks for", canonicalJson(claimed) === canonicalJson(expected)),
    check("agent key id is the hash of the public key", hex(await sha256(publicKey)) === keyId),
    check("buyer's ML-DSA-65 signature covers the cheque", verify(publicKey, signature, chequeMessage(expected))),
    check("passport type", body.type === PASSPORT_TYPE),
    check(
      "owner signature (ML-DSA-65) valid",
      verify(ownerKey, fromBase64(passport.signature), signedBytes),
    ),
    check("passport hash matches the receipt", passport.hash === hex(await sha256(signedBytes))),
    check("passport is for this vault and key", body.vault === vault && body.agent?.key_id === keyId),
  ];
  let withinCap = false;
  try {
    withinCap = limits.token === request.token_id && BigInt(request.asset_amount) <= BigInt(limits.per_payment_max);
  } catch {}
  const payees = body.payees ?? {};
  let inValidity = false;
  try {
    const end = BigInt(limits.valid_until_ms);
    inValidity = end === 0n || BigInt(request.created_ms) <= end;
  } catch {}
  checks.push(
    check("amount is within the passport's per-payment cap", withinCap),
    check(
      "payee is allowed by the passport",
      !payees.allowlist_enabled || (payees.accounts ?? []).includes(request.merchant_account),
    ),
    check("payment was inside the mandate's validity", inValidity),
  );
  return checks;
}

export function ownerHandle(receipt) {
  const key = fromBase64(receipt.passport.owner_public_key);
  return "ml-dsa-65-hash:" + base58(sha3_256(concat(enc.encode("near:ml-dsa-65-pubkey-hash:v1"), key)));
}

function events(outcome) {
  const out = [];
  for (const r of outcome.receipts_outcome ?? []) {
    for (const line of r.outcome?.logs ?? []) {
      if (!line.startsWith("EVENT_JSON:")) continue;
      try {
        const parsed = JSON.parse(line.slice("EVENT_JSON:".length));
        if (parsed && typeof parsed === "object") out.push(parsed);
      } catch {}
    }
  }
  return out;
}

/** Check the settlement `outcome` (a final transaction status from the RPC) against the receipt. */
export function verifyReceiptOnChain(receipt, outcome, ownerChainKeys = null) {
  const cheque = receipt.cheque;
  const keyId = receipt.approval.key_id;
  const failure = outcome.status?.Failure;
  const checks = [
    check("transaction succeeded", !failure, failure ? JSON.stringify(failure).slice(0, 200) : ""),
    check("transaction hash matches", outcome.transaction?.hash === receipt.settlement.tx_hash),
  ];
  const vaultEvents = events(outcome).filter((e) => e.standard === "wayfarer_vault");
  const authorised = vaultEvents.filter((e) => e.event === "payment_authorized").map((e) => e.data);
  const settled = vaultEvents.filter((e) => e.event === "payment_settled").map((e) => e.data);
  const match = authorised.find((d) => d.intent_id === cheque.intent_id);
  checks.push(check("vault emitted payment_authorized for this cheque", match !== undefined));
  if (match !== undefined) {
    checks.push(
      check("on-chain receipt hash is the request commitment", match.receipt_hash === cheque.receipt_hash),
      check(
        "on-chain payee, token and amount match",
        match.to === cheque.to && match.token === cheque.token_id && match.amount === cheque.amount,
      ),
      check("on-chain signer is the ML-DSA agent key", match.agent_key === `ml-dsa-65:${keyId}`),
    );
  }
  checks.push(
    check(
      "vault emitted payment_settled (transfer completed)",
      settled.some((d) => d.intent_id === cheque.intent_id),
    ),
  );
  if (ownerChainKeys !== null) {
    const handle = ownerHandle(receipt);
    checks.push(check("owner key is on the owner account on-chain", ownerChainKeys.includes(handle), handle));
  }
  return checks;
}

const DEFAULT_RPC = { testnet: "https://rpc.testnet.near.org", mainnet: "https://rpc.mainnet.near.org" };

async function rpc(url, method, params, fetchImpl) {
  const response = await fetchImpl(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: "wayfarer-verifier", method, params }),
  });
  const json = await response.json();
  if (json.error) throw new Error(JSON.stringify(json.error).slice(0, 200));
  return json.result;
}

/** Fetch the public chain data a receipt refers to, then run the on-chain checks. */
export async function verifyReceiptOnline(receipt, { rpcUrl, fetchImpl = globalThis.fetch } = {}) {
  const url = rpcUrl ?? DEFAULT_RPC[receipt.network];
  if (!url) return [check("known network", false, String(receipt.network))];
  try {
    const outcome = await rpc(
      url,
      "tx",
      { tx_hash: receipt.settlement.tx_hash, sender_account_id: receipt.settlement.relayer, wait_until: "FINAL" },
      fetchImpl,
    );
    const keys = await rpc(
      url,
      "query",
      { request_type: "view_access_key_list", finality: "final", account_id: receipt.passport.body.owner },
      fetchImpl,
    );
    return verifyReceiptOnChain(receipt, outcome, keys.keys.map((k) => k.public_key));
  } catch (error) {
    return [check("fetched the transaction and the owner's keys from the RPC", false, String(error))];
  }
}
