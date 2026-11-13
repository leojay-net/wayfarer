# wayfarer-vault: on-chain spending policy for AI agents

**Status: prototype. Tested locally (unit tests + NEAR sandbox) and deployed to NEAR TESTNET only (see [TESTNET-DEPLOYMENT.md](TESTNET-DEPLOYMENT.md)); not on mainnet. NOT audited. Do not hold funds you cannot lose.**

Code: `contracts/wayfarer-vault/` (Rust, `near-sdk` 5.30). Build and test:
```bash
cd contracts/wayfarer-vault
cargo build --target wasm32-unknown-unknown --release   # .cargo/config.toml adds the required `--cfg near`
cargo test --lib             # 40 unit tests, mocked environment
cargo test --test sandbox    # 4 tests on a local NEAR sandbox, neard 2.14.1 (downloads the binary on first run; 1 needs network)
cargo test --test delegate   # 2 tests: NEP-366 delegate actions against the contract (see SPIKES.md)
```

## Why it exists
An AI agent that can move money is only as safe as the rule that bounds it. A prompt is not a rule. The vault puts the rule on-chain, in the account that holds the funds, so a hallucinating, jailbroken or compromised agent *cannot* exceed it.

## Model
- The vault is its own NEAR account holding the funds. The **owner** (a normal NEAR account) administers it.
- The agent gets a **NEAR function-call access key** on the vault account, added by `add_agent`. The runtime restricts it to the methods `pay_direct` and `pay_via_intents` on this contract, with a gas allowance. That key cannot withdraw, change policy, add keys or call anything else; the NEAR runtime itself rejects such calls.
- The contract additionally checks that `predecessor == this account` and that the signing public key is a registered agent key, so another contract cannot replay calls.
- **Policy** (set by the owner, per token): `per_tx_max` and `daily_max` in the token's smallest unit. `token_id` is `"near"` or an FT contract id. A token without a policy cannot be paid at all.
- Extras: optional recipient **allow-list** (applies to `pay_direct`), a **pause** switch, one-time **intent ids** (replay protection), instant **revoke** (deletes the access key).
- Every authorisation emits a NEP-297 event (`standard: "wayfarer_vault"`) including the agent-supplied `receipt_hash` (intended: SHA-256 of transcript + parsed intent), so a user can later prove what was authorised.
- If the token transfer fails, a callback restores the daily budget and frees the intent id so it can be retried.

## What is enforced on-chain, and what is not
| Property | Enforced? |
|---|---|
| Agent cannot exceed per-tx / daily caps | **Yes**, tested on sandbox and on testnet (native NEAR path) |
| Agent cannot change policy, withdraw, or add keys | **Yes**, tested on sandbox |
| Revoked key can no longer pay | **Yes**, tested on sandbox |
| Same intent id cannot be paid twice | **Yes**, tested on sandbox |
| Recipient allow-list on `pay_direct` | Yes, unit-tested only |
| Panic messages reach the client | Yes, after adding `--cfg near` (see below) |
| `pay_via_intents`: *final* recipient | **No.** The 1Click deposit address hides the destination; `declared_recipient` is only logged. The caps still apply. Binding the final recipient needs on-chain verification of the 1Click quote signature; not built. |
| FT (e.g. USDC) transfer and failure rollback | Unit-tested with mocked promise results only; **not yet run against a real FT contract** |
| Owner key compromise | Out of scope: the owner can drain the vault by design |

## Known limitations
- The daily window is a UTC day, so up to 2x `daily_max` can move across a day boundary.
- FT recipients must already be registered with the token contract (storage deposit) or the transfer fails and the budget is restored.
- `gas_allowance_near` on the key bounds gas only; it is not a spending limit.
- Build gotcha found while testing: with `near-sdk` 5.30 and Rust 1.99, building without `--cfg near` produces a wasm whose panics are bare `unreachable` traps with no message. `.cargo/config.toml` in the contract directory sets the flag.

## v2: quantum-safe cheques and mandate terms (13 Nov 2026, sandbox-tested; testnet status in TESTNET-DEPLOYMENT.md)

**Status: prototype, unaudited.** Needs nearcore protocol 87+ (the `ml_dsa_verify` host function); testnet and mainnet are on 88.

New methods (the v1 methods are unchanged):
| Method | Who | What |
|---|---|---|
| `add_pq_agent(public_key, expires_at_ms, min_interval_ms)` | owner | Registers an ML-DSA-65 agent key (raw 1,952 B, base64) in contract state; returns its `key_id` (hex SHA-256 of the key). `expires_at_ms` = end date of the mandate (0 = none); `min_interval_ms` = minimum time between this key's payments (0 = none). |
| `revoke_pq_agent(key_id)` | owner | Removes the key at once; cheques it already signed can no longer settle. |
| `pay_with_cheque(intent_id, token_id, to, amount, expires_at_ms, receipt_hash, key_id, signature)` | **anyone** (relayer pays gas) | Verifies the ML-DSA-65 signature over the cheque message, then applies the same policy as the other paths. |
| `cheque_message(...)` | view | The exact bytes to sign; used to test that wallets and contract agree. |
| `get_mandate(key_id, token_id)`, `get_pq_agent`, `list_pq_agents` | view | The terms one key operates under: expiry, interval, last payment, caps, allow-list, owner, paused. |

The cheque message is `"wayfarer-cheque-v1"` followed by the borsh encoding of `vault, token_id, to, amount (u128), intent_id, expires_at_ms (u64), receipt_hash`. It binds the vault account (no cross-vault replay), payee, amount and expiry. Python builds the same bytes in `src/wayfarer/cheque.py`; `contracts/wayfarer-vault/tests/fixtures/cheques.json` (generated by `scripts/make_cheque_fixtures.py`) is signed by the Python signer and verified by the contract's tests with the real host function.

Checks, in order: cheque not expired; key registered; mandate not expired; minimum interval elapsed; allow-list (if enabled); signature valid; then the shared policy (not paused, per-tx cap, daily cap, one-time `intent_id`). If the token transfer fails, the callback restores the budget, the intent id **and** the key's interval slot. Events: `payment_authorized` now also carries `relayer` and, for cheques, `agent_key: "ml-dsa-65:<key_id>"` with `kind: "cheque"`.

Tested: valid cheque pays through a non-agent relayer; replay, wrong key, changed amount or payee, expired cheque, expired mandate, minimum interval (both sides), revoked key, paused vault, allow-list, caps, owner-only registration, key-size checks, rollback after a failed transfer (unit); the full flow in the NEAR sandbox with nearcore's own ML-DSA signer, including gas (7.2 Tgas for a native-NEAR cheque payment).

Not covered / honest limits:
- Cheque settlement is only as final as the on-chain transaction: an offline cheque can still fail at settlement (balance spent elsewhere, key revoked, mandate ended, daily cap used up). See PLAN-TAP.md.
- `expires_at_ms` and `min_interval_ms` apply to the ML-DSA agent keys only; classic ed25519 agent keys (v1 path) keep v1 behaviour.
- The ML-DSA signature check relies on the protocol's host function; there is no second implementation in the contract. The Python signer is a non-constant-time reference.
- FT (USDC) cheques use the same unit-tested-only transfer path as v1.
- State layout changed (new field), so v2 cannot be deployed over a v1 vault's state; it needs a fresh account (or a migration, not written).
