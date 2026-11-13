# Spikes (plan step 1): results, including failures

Run 13 Nov 2026. Everything here is a local NEAR sandbox or testnet result; nothing is on mainnet.

## 1a. Can a restricted function-call key sign a DelegateAction for `pay_direct`? (sandbox)

Test: `contracts/wayfarer-vault/tests/delegate.rs` (`cargo test --test delegate`). The transaction is built with nearcore's own crates (`near-primitives` 0.36), so it does not depend on our Python encoder. Setup: vault account is the delegate `sender_id` and `receiver_id`; the agent's restricted function-call key signs; a third account (the relayer) signs and pays for the outer transaction.

| Question | Result |
|---|---|
| Does the protocol accept a delegate action signed by a function-call key limited to `pay_direct`/`pay_via_intents` on the vault? | **Yes.** The signed delegate verifies, the outer transaction succeeds and the inner call is executed on the vault. |
| Does the key's method restriction still apply inside a delegate action? | **Yes.** A delegate action calling `set_policy` is refused by the runtime (`MethodNameMismatch`) before any contract code runs. Test: `function_call_restriction_holds_inside_a_delegate_action`. |
| Is `predecessor_account_id` the vault? | **Yes** (diagnostic build, see below). |
| Does `only_agent` see the delegate key via `env::signer_account_pk()`? | **No. This disproves the plan's assumption.** In the delegated call, `signer_account_id` and `signer_account_pk` are the **relayer's**, not the delegate key's. The v1 `only_agent` therefore refuses every delegate payment ("Only a registered agent key can call this"). Test: `v1_refuses_delegate_payment_because_the_signer_key_is_the_relayers` pins this for the v1 contract; no funds move. |

How the signer fields were established: a temporary diagnostic build appended `predecessor`, `signer_account_id` and `signer_account_pk` to the panic message. Output: predecessor = the vault account, signer = the relayer account, signer key = the relayer's key (not the delegate key that appears in the transaction's `public_key` field). The diagnostic was reverted; it is not in the repo.

Consequence for the design (contract v2, plan step 3):
- On the delegate path the **protocol** authenticates the key. The runtime has already checked that `delegate_action.public_key` is an access key of the vault, that its nonce is fresh, that `max_block_height` has not passed, and that the call matches the key's `receiver_id` and method list. The contract cannot see which key it was; it only sees `predecessor == vault` and `signer != vault`.
- Therefore v2 `only_agent` must accept two shapes: (a) direct call, `signer == vault` and the signer key is a registered agent; (b) delegated call, `predecessor == vault` and `signer != vault`. The only ways to get shape (b) are a delegate action signed by a key on the vault account, or the vault's own promises (it makes none to these methods).
- What is lost: the contract cannot log *which* agent key paid on the delegate path. v2 events should record `via: "delegate"` and the relayer account instead. `revoke_agent` still works because deleting the key makes the protocol refuse its delegate actions.
- This also means contract-level verification of ML-DSA is not needed or possible here: the protocol verifies the signature (NEP-645), which is the reason to use NEP-366.

Side observation: the vault's balance rises slightly when a relayer calls it, because contracts earn a share of the gas burned. The relayer, not the vault, pays gas.

Not yet tested (needs v2): a successful delegate payment end to end, replay of the same signed delegate action, expiry by `max_block_height`, and a revoked key. These are in the step 3 test list.

## 1b. ML-DSA-65 function-call key on testnet (13 Nov 2026)

Tool: `tools/pqref` (Rust, built on nearcore's own `near-crypto` 0.38.1, which wraps `aws-lc-rs` ML-DSA-65). Run: `pqref testnet-spike <ed25519-key-file> <account> <seed-hex> <receiver> <method>`. The ML-DSA seed is stored outside the repo (`~/.near-credentials/wayfarer-pq/`).

| Check | Result |
|---|---|
| RPC `protocol_version` on testnet | **88** (>= 85 needed) |
| Key sizes from the reference crate | public key 1,952 B, signature 3,309 B (as in NEP-645); string form `ml-dsa-65:<base58>` |
| `AddKey` of an ML-DSA-65 **function-call** key (receiver = vault, method `pay_direct`, allowance 0.1 NEAR), signed by the account's ed25519 key | **accepted**, tx `CDyiucnsxDLWXhiK1VLM25AaaJXssXUFm3sQ7dKNVdyq` (wire size 2,263 B) |
| A transaction **signed by that ML-DSA key** (FunctionCall `pay_direct` on the vault) | **accepted and executed by the protocol**, tx `G4RKwvrGCuRrrbjrczkiXRqqwGkjAJLHeapHpNmTxRUi` (wire size 5,556 B; tx outcome burnt 0.408 Tgas). The signer key in the transaction is the `ml-dsa-65:` key. The contract then refused the call (`Only a registered agent key can call this`) because the old v1 contract has not registered this key as an agent. That refusal is expected and shows the signature itself passed the protocol check. |
| Clean-up `DeleteKey` | ok, tx `2JrajMK8oQNd9fqifyrgTFemp7qwSkMS2E4Wj9qb7kfy`. The ML-DSA key is no longer on the vault. |

Findings and caveats:
- NEP-645 (as read on 13 Nov) does **not** state the signing context, whether signing is deterministic or hedged, or the transaction hash; nearcore's `near-crypto` signs the 32-byte transaction hash (same as ed25519) with `aws-lc-rs` `PqdsaKeyPair::sign`. Our Python signer (plan step 2) must match that exactly and will be cross-checked against `pqref` with known-answer and cross-verification tests. Verification does not depend on determinism.
- Borsh: key-type tag 2 for both `PublicKey::MLDSA65` and `Signature::MLDSA65`, followed by the raw bytes with no length prefix.
- A transaction signed with ML-DSA is about 5.5 KB on the wire (vs ~250 B for ed25519). Relevant for the offline cheque size in the tap protocol: the signed delegate action alone is over 5 KB (1,952 B key + 3,309 B signature + body).
- This spike ran against the **old** vault wasm and added/removed a key on the real testnet vault account; nothing was paid.
- Not yet tested: `add_agent` with an ML-DSA key (the v1 contract's `PublicKey` parsing may not accept it), an ML-DSA key as a DelegateAction signer, and the on-chain handle (SHA3-256 of the key) as shown by `view_access_key`/key listing.

## 1c. Plan change found while building contract v2 (13 Nov 2026): verify ML-DSA inside the contract

Two facts changed the design of the "cheque":
1. `near-sdk` 5.30's `PublicKey` cannot hold an ML-DSA key (only the classic curves), so `add_agent` cannot register an ML-DSA key as an access key, and `revoke_agent` could not delete one. An ML-DSA agent key added the 1b way (a raw `AddKey` by a full-access key) is invisible to the contract.
2. Delegate actions hide the signing key from the contract (finding 1a).

But `near-sdk` 5.30 exposes the `ml_dsa_verify` host function (nearcore protocol 87+, neard 2.14; cost 540 Ggas + 11 Mgas per message byte; message verified as-is with an **empty context**, no pre-hash). Testnet and mainnet are on protocol 88. This also settles what NEP-645 left open: the context is empty.

So contract v2 does not use NEP-366 for the cheque. The vault stores the ML-DSA-65 agent key **in contract state** (`add_pq_agent`), the buyer's phone signs a canonical cheque message, and **anyone** can submit it (`pay_with_cheque`); the contract verifies the signature itself. Benefits over the delegate-action plan: the contract knows exactly which key authorised each payment (event `agent_key: ml-dsa-65:<key_id>`); the cheque is ~3.5 KB (no 1,952 B public key inside it) instead of ~5.4 KB; revocation and mandate terms are plain contract state; the agent key is not an access key, so it cannot do anything outside the contract. The delegate-action code (`near/transaction.py`, `tests/test_delegate.py`, `contracts/wayfarer-vault/tests/delegate.rs`) stays as a verified alternative and as the way the relayer's own transaction is encoded; it is not on the payment path.

Measured in the sandbox (neard 2.14.1): one `pay_with_cheque` payment of native NEAR, including verification, transfer and callback, burned 7.2 Tgas in total (`CHEQUE-GAS` line of `cargo test --test sandbox -- --nocapture`).

Side effect on testing: the sandbox binary that `near-workspaces` 0.22 downloads (neard 2.13.4) does not know `ml_dsa_verify`, so even deploying v2 failed with `Link Error: unknown or invalid import`. Dev-dependencies moved to `near-workspaces` 0.24 (neard 2.14.1).
