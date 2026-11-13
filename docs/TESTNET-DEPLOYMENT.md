# Testnet deployment (12 Nov 2026) — prototype, unaudited, testnet only

| Item | Value |
|---|---|
| Network | NEAR **testnet** (no real value) |
| Owner account | `wayfarer-vault-11121649.testnet` (created via the testnet faucet service; key stored outside the repo in `~/.near-credentials`) |
| Vault contract | `vault.wayfarer-vault-11121649.testnet` |
| Code hash (SHA-256) | `8e2f908325616d0677b6574d09c18f0cd1e1c600e8831e414a217585ecfba19a` (matches the local release build of `contracts/wayfarer-vault`) |
| Policy | native NEAR: 0.5 NEAR per transaction, 1 NEAR per UTC day |
| Agent key | `ed25519:4rBDxqNXHH6FZh8smKV1StTC1sGvP81SC1dAARKLqvix`, on-chain as a function-call key limited to `pay_direct` and `pay_via_intents` on the vault, gas allowance 0.5 NEAR. The private key is stored outside the repo. |

## Transactions (explorer: `https://explorer.testnet.near.org/transactions/<id>`)
| Step | Result | Transaction |
|---|---|---|
| Deploy + `new` | ok | `ypnbURnFrvttghJGuk1Xb9qPk77hJg3kouvrC17UmBF` (an earlier deploy `HrFjwrrUCNmiK8S2EbLpBEgeNYQLXDvsFRqJ1pSyHLeV` failed: not enough balance for the 450 KB wasm's storage) |
| `set_policy` (owner) | ok | `FSDPpiPSCnSRiiYVJD5NXSwnSDS3VKduxg7aZAGPh5ze` |
| `add_agent` (owner) | ok | `F3jsgQhiVyFZu287wJT5EuSfyVQMsKT4evsstYuk9XE4` |
| Agent `pay_direct` 0.3 NEAR (within limit) | **paid**; events `payment_authorized` (with receipt hash) and `payment_settled` | `GE6EmJ7FyJ6RnyvzRALSCQynMf6oao8vFWrm5Q4vdfVS` |
| Agent `pay_direct` 0.6 NEAR (over limit) | **refused**: `Smart contract panicked: Over the per-transaction limit` | `HQM2Q1sq3wAthKmtHnYoegDuqoJaDZ293gFE6W6Xxv2D` |
| Agent `set_policy` | **rejected before execution** by the runtime: `Transaction method name <set_policy> isn't allowed by the access key` | no transaction was created |

After these, `get_budget("near")` returned spent 0.3 NEAR, remaining 0.7 NEAR.

## Python agent (same vault, same agent key), 12 Nov
`src/wayfarer/near/` (keys, transaction, rpc) + `src/wayfarer/vault.py` sign and send `pay_direct` themselves (no CLI). Live results:
| Request | Result | Transaction |
|---|---|---|
| 0.05 NEAR | paid (`payment_settled` event seen) | `7NVRwHRa2iXwnshhdTGtSYXis4QeqUz2uLEchw1Fp8sa` |
| 0.6 NEAR | refused: `Over the per-transaction limit` | `CoECxLTLSUmkZoXQwgFptFinDjS3MhBpg17JH6PimXgC` |
| 1 NEAR | refused: `Over the per-transaction limit` | `GQ4HAZUX6csiHRscUNZv3BjNoJY64ZqJT92ieuaFZZxs` |
| 5 USDC | not sent; reported unsupported (no USDC path on testnet) | none |

Budget afterwards: 0.35 NEAR spent today, 0.65 remaining. These calls were made by a script, not yet from a Telegram Confirm tap by a human.

## What this does NOT show
Only the native-NEAR path was exercised on testnet. The FT (USDC) path and `pay_via_intents` against a real 1Click deposit address have not been run on-chain. The recipient in the demo payment was the owner account, not an external party. No audit.

## Reproduce
Needs the NEAR CLI (`cargo install near-cli-rs --locked`; on an arm64 Mac with an Intel Homebrew `libgit2` set `LIBGIT2_NO_PKG_CONFIG=1`). See `scripts/deploy_vault.sh`.
