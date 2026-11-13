# Quantum-safe vault on NEAR testnet (13 Nov 2026): prototype, unaudited, testnet only, worthless funds

Everything below was run by `scripts/pq_testnet.py` (Python, our own ML-DSA-65 signer and transaction encoder) against `https://rpc.testnet.near.org` (protocol 88). Explorer: `https://explorer.testnet.near.org/transactions/<id>`.

## Accounts
| Role | Account | Notes |
|---|---|---|
| Owner | `wfpq-owner-9bda7c.testnet` | writes the mandate |
| Vault | `wfpq-vault-9bda7c.testnet` | contract v2, code hash `a747b7f743c4851a6dc201f54f55fa709787dd8a501bc73ec3bfc7c0089b4dff` (SHA-256, **equals** the local release build and the on-chain hash read back over RPC) |
| Shop | `wfpq-shop-9bda7c.testnet` | payee and relayer: submits cheques and pays the gas |

Created with the testnet faucet service (10 NEAR each, valueless) and the NEAR CLI: `near account create-account sponsor-by-faucet-service wfpq-<role>-9bda7c.testnet autogenerate-new-keypair save-to-legacy-keychain network-config testnet create`, then `near contract deploy wfpq-vault-9bda7c.testnet use-file <wasm> with-init-call new json-args '{"owner_id":"wfpq-owner-9bda7c.testnet"}' ...` (tx `7ycWYkS3P5XW9fMchPHWqyL3SwqUkAePDjWzhk1H4amf`). The faucet gives each account an ed25519 full-access key, which `rotate` then removes.

## Result 1: the accounts hold only ML-DSA-65 keys (`rotate`, then `report`)
For each account an ML-DSA-65 full-access key was added by the ed25519 key, then the ed25519 key was **deleted by a transaction signed with the ML-DSA key**. The chain lists a single key per account, shown as a 32-byte handle (`ml-dsa-65-hash:...`, SHA-256-style trie handle per NEP-645; the handle our code computes for the shop key equals the one listed):

| Account | AddKey ML-DSA (signed by ed25519) | DeleteKey ed25519 (signed by ML-DSA) | Keys listed afterwards |
|---|---|---|---|
| owner | `EsWd86BWjkxxL1WYipaVnTW6LrxgWm3414r9F2WbSecs` | `4CSC4xMBM5dajM4TsXhSChS5fjdLjTKefFur8BCwLG9S` | `ml-dsa-65-hash:AS7LEN9e...` (full access) |
| vault | `BrwqyeE1hmPz4xawWF41dqT3A4nqCD8NyAPPuuWqUh66` | `6vYhMFNpXXBCekAzs1UiiBwnrXkneLJDF8Yf4qBhTUdc` | `ml-dsa-65-hash:3gymMen4...` (full access) |
| shop | `FCcYXUTazaTFAmQEJLBiUPr7tJdPWzeN2CiMW3P5Cbvu` | `7y3446HFRrsTx38fzJ8rp65XJwRsVaerekgzmW5xjHE` | `ml-dsa-65-hash:6WUQtSdp...` (full access) |

Consequence: nobody holds an ed25519 key for any of the three accounts any more; the vault contract cannot be redeployed (its only key is ML-DSA and it has no upgrade method). What this does **not** make quantum-safe: validators and consensus stay classical, and the NEAR AI Cloud TEE signatures are still ed25519.

## Result 2: mandate written by the owner (`setup`)
Owner (ML-DSA-signed transactions): `set_policy` near, 0.5 NEAR per payment, 1 NEAR per UTC day: `5C8LJUFZZaPcvBt2NmrsqrLkfqvCANiTHdarJGX2mNcY`. `add_pq_agent` for the buyer's ML-DSA key (`key_id` `953afe29...2691`), ends in 7 days, 5 s minimum interval: `C5cNzjSKkss1CspUTNp35uPRvYLLm28vh7mQmUxrAHz2`.

## Result 3: a cheque is paid (`pay`)
The buyer's key signed a 0.05 NEAR cheque to the shop; the **shop's** account submitted it: `C7CHcSDMMiX95awAPupZDmTiTheUpdh9LrUEddeC83LF` -> `payment_settled`, shop balance +0.049318 NEAR (0.05 minus its own gas). The contract verified the ML-DSA-65 signature with the `ml_dsa_verify` host function.

## Result 4: refusals (`refusals`, `interval`), all by the contract
| Case | Outcome | Transaction |
|---|---|---|
| Second valid cheque | paid | `DL8qFaGGsRDJWR3R1QyE3eYn6Qhv9yjixi3WNaUV3xGK` |
| Replay of that cheque | refused: `intent_id already used` | `AmrHZy3y8gxiDjLvV443YQr2kdT7BkkVBSEmhX3f3KLf` |
| Validly signed 0.6 NEAR (cap 0.5) | refused: `Over the per-transaction limit` | `F1Y1AFrCEdorwngAEZwAaLU62XWziqHPQLoNeKHntmJV` |
| Signature for 0.05 NEAR presented for 0.5 NEAR | refused: `Invalid cheque signature` | `8zCqZKi2P5NzBkTEkZH7TAfbaDfXLfGSf2rSRkuDRvsJ` |
| Cheque past its own expiry | refused: `Cheque has expired` | `5Ur7uRYsV6Y66yHkfAczSaEwqwt6xxQ9dY3uv3LiPhXn` |
| Owner revokes the buyer key (`4ragw4EsV4WiowKe7UGrxgLD6rsZP44uVnKfjDYEPmoH`), then a fresh valid cheque | refused: `Unknown agent key` | `7HYyRw1NNuZFgNhQkYgpjpXqSgfaSko1toSiik9iCnXw` |
| Minimum interval (second key, 120 s): first cheque | paid | `Ch5SgLnquY8PUjNvfsPHTFVmdzxSPYrdC4i8WFLkpdJz` |
| Same key, second cheque right after | refused: `Minimum interval between payments has not elapsed` | `BUkxwPB8Be3stxanj9U1FmpLMamtU8fQDpqQehwi1Stz` |

`get_mandate` for that key returned the terms (vault, owner, `min_interval_ms` 120000, `last_payment_ms`, caps 0.5 / 1 NEAR, no allow-list).

### A mistake worth recording
The first run of `refusals` expected a **5 s** minimum interval to block an immediate second cheque, but the second cheque was **paid** (`FuSY5JhtbaG2xQ1wYwf7bqKxaWpoMuAd3F1LgDSPHzFV`). Cause: each submission waits for finality, which takes several seconds, so 5 s had already elapsed; the contract was right and the demo was wrong. The interval is covered by unit tests on both sides and, with a 120 s interval, by the last two rows above. A 5 s interval is therefore not a meaningful tap-rate limit on this chain; use tens of seconds or more.

## What this does not show
- Only native NEAR, only testnet, only throwaway accounts. No FT/USDC cheque, no mainnet, no audit.
- The cheques were produced and submitted by a script on one computer, not by two phones. The offline-tap link, the Android signer and its timing are untested.
- ML-DSA signing speed in this pure-Python signer on a phone is unknown (a CPython laptop signs in well under a second; no phone measurement exists).
- The vault pays nothing for a cheque that fails, but a cheque can fail at settlement for reasons the buyer's phone cannot see offline (empty float, revoked key, ended mandate, daily cap used by other cheques). See PLAN-TAP.md.

## Reproduce
After creating and deploying the three accounts as above: `python3 scripts/pq_testnet.py --suffix <suffix> rotate | setup | pay | refusals | interval | report`. ML-DSA seeds are written (mode 600) to `~/.near-credentials/wayfarer-pq/`, outside the repository.
