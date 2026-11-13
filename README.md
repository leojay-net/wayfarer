# Wayfarer

**Spend naira, hold NEAR.** A buyer taps their phone on a merchant's phone, approves a price shown in naira, and a **quantum-safe (ML-DSA-65), policy-bounded, human-approved** payment is made from their on-chain vault. The merchant can cash out toward a bank with one button. Both sides get a receipt that anyone can check in a browser.

**Status: working prototype on NEAR *testnet* (valueless funds). Unaudited. Not on mainnet. Not yet run on two real phones.** Read "What is real, what is simulated" before judging anything. Everything stated as working below was run; each claim names the command or file that shows it.

## The flow

1. **Merchant** enters NGN 500. The app locks a rate for 60 s (a hand-typed DEMO rate, no market feed) and shows a *PaymentRequest*: merchant account, naira amount, NEAR amount (rounded **up**), quote id, expiry, nonce.
2. **Tap.** NFC (merchant phone = card, buyer phone = reader) or an animated QR as the always-works fallback.
3. **Buyer** sees "Pay NGN 500.00 to Ada's Shop = 0.100000 NEAR", confirms with fingerprint/PIN, and the phone signs a **cheque** with an **ML-DSA-65** key. That key is registered in the vault with a mandate (per-payment cap, daily cap, end date, minimum interval, optional payee allow-list). It cannot withdraw or change policy.
4. **Merchant** checks the cheque offline (signature, amount, request hash) and shows **"accepted, pending settlement"**. This is *not* payment.
5. **Settlement.** The merchant (or any relayer) submits `pay_with_cheque`. The vault contract verifies the ML-DSA signature **on-chain** with the NEAR `ml_dsa_verify` host function, enforces the mandate, emits `payment_authorized` / `payment_settled`, and records the one-time payment id. It is paid only when that event is final.
6. **Cash-out.** NEAR -> USDC -> bank, behind one button, through a `SwapLeg` and a `Rail`. Only **sandbox** implementations exist (see limits).
7. **Receipt.** A JSON receipt links the request, the buyer's signature, the owner-signed **Mandate Passport** and the on-chain events. `verifier/index.html` re-checks all of it in the browser.

## What is real, what is simulated

| Piece | Real | Simulated / not done |
|---|---|---|
| ML-DSA-65 (FIPS 204) | Own pure-Python signer (`src/wayfarer/crypto/mldsa.py`), own JS verifier (`verifier/mldsa.js`), Kotlin via Bouncy Castle 1.79. Public keys and signatures are byte-identical with nearcore's `near-crypto` (`tests/vectors/mldsa65.json`, made by `tools/pqref`) | Reference signers are not constant-time. Do not protect real value with them |
| Accounts with only ML-DSA keys | Three testnet accounts whose ed25519 keys were deleted, transactions signed with ML-DSA (NEP-645), listed on-chain as `ml-dsa-65-hash:...`: [docs/TESTNET-PQ.md](docs/TESTNET-PQ.md) | Validators and consensus stay classical. NEAR AI Cloud TEE signatures are still ed25519 |
| Vault contract v2 (`contracts/wayfarer-vault`) | `pay_with_cheque`, ML-DSA agent keys, mandate, `get_mandate`, rollback of budget on a failed transfer. 40 unit + 4 sandbox + 2 delegate tests pass. Deployed to testnet as `wfpq-vault-9bda7c.testnet` | Native NEAR only for cheques. Unaudited |
| Settlement on testnet | Paid cheques, replay / over-cap / forged / expired / revoked / too-soon refusals, each a testnet tx: [docs/TESTNET-PQ.md](docs/TESTNET-PQ.md). A fresh run with a real receipt: [docs/examples/tap-receipt](docs/examples/tap-receipt) (tx `AimPaHPTXaYovVAKA9eLMqCh2eSqpXdcoap6brYPU4BN`) | The cheques were made and submitted by scripts on one computer |
| Tap protocol | Messages, strict codec, rate lock, envelope, QR frames, buyer wallet, merchant terminal in Python (`src/wayfarer/tap/`) and Kotlin (`wayfarer-tap/`); both reproduce the same bytes from `tests/vectors/tap.json` | Both phones are simulated in `scripts/tap_demo.py` |
| Android app (`wayfarer-app/`) | Debug APK builds (`./gradlew :wayfarer-app:assembleDebug`) and **launches on the Android 36 emulator**. NFC card/reader code uses the unit-tested APDU state machine | **Never run on two real phones.** NFC throughput for a 5.4 KB cheque, the QR scan loop, the biometric prompt and the Keystore path are untested on hardware. Nearby/Bluetooth not built. The merchant phone does **not** submit transactions: it exports the accepted cheque and `scripts/settle_cheque.py` settles it |
| Mandate Passport + receipt v2 | Passport signed by the owner's ML-DSA key; 20 checks (offline + on-chain) in Python and in JavaScript with the same check names; verified on the real testnet receipt | `verifier/index.html` was tested only through Node and a faked RPC, **not opened in a browser**. The passport is inspired by AP2 / UK commercial VRP and is **not compliant** with either |
| Cash-out | `CashOutService`, `SwapLeg`/`Rail` interfaces, a NEAR Intents 1Click quote leg, sandbox legs (`src/wayfarer/cashout/`, [docs/CASHOUT.md](docs/CASHOUT.md)) | **No swap executed, no bank paid, no money moved.** The rail is a sandbox ledger. No licensed partner is integrated |
| Rates | 60 s lock, round-up maths | The rate is typed in by hand (DEMO), not a market feed |

## Honest limits (please read)
- **Offline acceptance is not final.** Until `pay_with_cheque` settles, the cheque can fail: empty vault, revoked key, ended mandate, daily cap used by other cheques. Mitigations that exist: a per-tap limit on the phone, a short expiry, and an online *strong check* (`MerchantTerminal.strong_check`) that reads the vault. Residual risk remains.
- Android only (iOS restricts card emulation). Rate risk: the lock covers the cheque window only; keep amounts small.
- A 5 s minimum interval does not rate-limit on this chain (finality takes several seconds); we recorded this mistake in [docs/TESTNET-PQ.md](docs/TESTNET-PQ.md).
- A receipt does not prove which human pressed the button, or that goods were delivered. Sharing one reveals the merchant and the naira amount.
- Android's Keystore has no ML-DSA: the seed is stored encrypted under a Keystore AES key that needs a fresh unlock, but it sits in app memory while signing.
- Cashing out to a Nigerian bank needs a licensed partner; we do not custody or convert fiat.
- Nothing here has moved real money. No mainnet run has been made.

## NEAR features used (and nothing else)
- **Smart contract + NEP-297 events**: the vault (`contracts/wayfarer-vault`), testnet.
- **ML-DSA-65 accounts and signatures (NEP-645)**: transaction signing by ML-DSA keys, and the `ml_dsa_verify` host function (protocol 87+) inside `pay_with_cheque`.
- **NEP-366 delegate actions (NEP-461 hash)**: built, signed and sandbox-tested as a spike; the shipped cheque path does **not** use them (the design changed, see [docs/SPIKES.md](docs/SPIKES.md)).
- **NEAR JSON-RPC**: transactions are borsh-encoded and signed in Python (`src/wayfarer/near/`).
- **NEAR Intents 1Click**: quote client used by the cash-out leg and the earlier `pay_via_intents` path; only dry quotes were made live, no swap was executed ([docs/INTENTS.md](docs/INTENTS.md)).
- **NEAR AI Cloud** (voice bot only, see below): Whisper, chat completions, completion signatures.

## Run it

Python 3.11+ (standard library only at runtime). For development only: `pip install -e '.[dev]'` (ruff, mypy), then `ruff check src tests scripts && mypy --strict src tests scripts`.

```bash
# 1. Python tests: protocol, ML-DSA, cheque, receipts, cash-out (no network, no keys)
python3 -I -m unittest discover -s tests -t .

# 2. The whole tap on two SIMULATED phones, offline (no keys, no chain)
python3 scripts/tap_demo.py --naira 500

# 3. Browser verifier tests (Node, tested with v24): real testnet receipt, tampering, faked RPC
node --test verifier/receipt.test.js
python3 -m http.server -d verifier 8000      # then open http://localhost:8000 and paste docs/examples/tap-receipt/receipt.json

# 4. Kotlin protocol + NFC state machine against the shared vectors (JDK 17)
./gradlew :wayfarer-tap:test
./gradlew :wayfarer-app:assembleDebug        # debug APK; needs the Android SDK (platform 36); downloads dependencies

# 5. Contract (Rust, wasm32 target; the sandbox tests download a neard binary on first run)
cd contracts/wayfarer-vault && cargo fmt --check && cargo clippy --all-targets && cargo test && cd ../..

# 6. Testnet (valueless funds; needs the ML-DSA seeds of docs/TESTNET-PQ.md in ~/.near-credentials/wayfarer-pq/)
python3 scripts/tap_demo.py --naira 500 --testnet 9bda7c --save /tmp/receipt   # pays, builds + checks a receipt, revokes the buyer key
python3 scripts/add_phone_key.py --suffix 9bda7c --public-key-hex <hex from the app>
python3 scripts/settle_cheque.py accepted.json --check-only                    # offline verify of a cheque the app exported
python3 scripts/settle_cheque.py accepted.json --suffix 9bda7c --save out/     # settle on testnet, write a checked receipt
```

**End-to-end suite** (`scripts/e2e_suite.py --help`). Free stages: `offline`, `tap`, `verifier`, `kotlin`, `quote`. Testnet only: `pq --pq-suffix 9bda7c`, `vault`. **Spends NEAR AI Cloud credit** (hard cap `WAYFARER_COST_CAP_USD`, default $2.00, estimates not an invoice): `eval`, `attest`, `whisper`. Provider errors are never counted as passes.

Measured on 13 Nov 2026 on the author's computer: Python 215 tests, Kotlin 18 tests, Node 6 tests, contract 40 + 4 + 2 tests, `mypy --strict` and `ruff` clean. Signing one cheque in pure Python took about 0.1 s on that laptop; **no phone measurement exists**.

## New (since 1 Nov) vs inherited
- **Everything in this repository was written for this hackathon** (since 1 Nov): the Python package `src/wayfarer/`, `tests/`, `contracts/wayfarer-vault/`, `tools/pqref/`, `verifier/`, `wayfarer-tap/`, `wayfarer-app/`, `scripts/`, `testdata/` and `docs/`.
- **Earlier milestone, still here:** the voice bot. A Telegram voice note or typed instruction is parsed by NEAR AI Cloud, read back, and after **Confirm** pays through the v1 vault `pay_direct` (`src/wayfarer/telegram/`, `intent.py`, `receipts.py`, `attestation.py`, `cost.py`; evidence in [docs/TESTNET-DEPLOYMENT.md](docs/TESTNET-DEPLOYMENT.md)). Its limits stay as documented there: on 12 Nov NEAR AI Cloud's Whisper endpoint hung during our tests, no human completed a voice-note -> Confirm round trip, and the 60-case intent set (`testdata/intents.jsonl`, self-authored) has not been run. Run it yourself with `scripts/e2e_suite.py --stage eval`.

## Repo map
| Path | What it is |
|---|---|
| `src/wayfarer/tap/` | Tap protocol: `codec`, `request`, `rates`, `envelope`, `buyer`, `merchant`, `link` (QR frames) |
| `src/wayfarer/{cheque,pqvault,mandate,proof}.py` | Cheque, on-chain admin/relayer, Mandate Passport, receipt v2 |
| `src/wayfarer/cashout/` | Cash-out service, swap legs, rails (sandbox + Intents quote) |
| `src/wayfarer/crypto/mldsa.py`, `near/` | ML-DSA-65, NEAR keys, transactions, delegate actions, RPC |
| `contracts/wayfarer-vault/` | Rust vault contract v2 and its sandbox tests |
| `tools/pqref/` | Rust cross-check against nearcore's `near-crypto` (vectors, tx and delegate checks) |
| `verifier/` | Static receipt verifier (own Keccak + ML-DSA verify in JS) and Node tests |
| `wayfarer-tap/` | Kotlin (JVM) protocol core + NFC APDU state machine; tests read `tests/vectors/` |
| `wayfarer-app/` | Android app (Compose) built on `wayfarer-tap` |
| `docs/` | `TWO-PHONE-TEST.md` (how to run the real tap; not done yet), `PLAN-TAP.md`, `SPIKES.md` (incl. plan changes and failures), `TESTNET-PQ.md`, `VAULT.md`, `CASHOUT.md`, `INTENTS.md`, `DEMO-SCRIPT.md` |

Licence: MIT, see `LICENSE`.
