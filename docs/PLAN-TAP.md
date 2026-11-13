# Wayfarer Tap: plan (13 Nov 2026)

One line: **spend naira, hold NEAR.** A buyer taps their phone on a merchant's phone, approves a price shown in their own currency, and a quantum-safe, policy-bounded, human-approved payment is made. The merchant can cash out to a traditional bank in one button. Conversion happens behind the scenes.

Merges the four chosen ideas: (1) quantum-safe keys, (2) phone-to-phone tap with offline cheques, (3) proof-of-approval receipts, (4) mandates that traditional finance recognises.

## Flow
1. **Merchant** enters an amount in local currency (e.g. NGN 1,500). The app locks a rate for 60 s and shows a **PaymentRequest**: merchant NEAR account, NGN amount, asset amount, rate-quote id, expiry, nonce, request hash.
2. **Tap.** NFC starts the session and carries the small request; larger data goes over the same NFC link in chunks, or over Bluetooth/Wi-Fi Direct (Android Nearby Connections), or as an animated QR as the always-works fallback. One `PaymentLink` interface, several transports, plus a loopback transport for tests.
3. **Buyer** sees "NGN 1,500 to Ada's Shop", confirms with biometric/PIN. The phone signs a **cheque**: a NEP-366 `DelegateAction` (sender = buyer's vault, one `FunctionCall` to `pay_direct`, nonce, `max_block_height` expiry) with a restricted, policy-bound **ML-DSA-65** key held in the Android Keystore-backed store. The key cannot withdraw or change policy.
4. The cheque goes back over the link. The merchant app checks it locally (signature, expiry, amount matches the request, request hash) and shows "accepted, pending settlement".
5. **Settlement.** The merchant app (or any relayer) submits the signed delegate action and pays the gas. The vault enforces the mandate on-chain, emits `payment_authorized`/`payment_settled`, and the nullifier blocks replay. The merchant sees "paid" only after the settled event at finality.
6. **Cash out.** One button: NEAR -> USDC through NEAR Intents (`swap.py`) -> a bank rail (NIP through a licensed partner; a sandbox rail for anyone to run). The merchant sees naira arrive; the swap is hidden.
7. **Receipt.** Both sides get a proof-of-approval receipt: commitment of the request, TEE-signed parse if voice/text was used, buyer approval bound to the request hash, mandate hash, on-chain event, bank reference. A static web page verifies it in the browser from public data.

## Honest limits (must stay in the README)
- Offline tap is **not final**. Until the cheque is settled on-chain it can fail (insufficient float, revoked key, expired). Mitigation: a per-tap limit (like contactless caps), an online "strong check" mode that reads the vault budget and key before accepting, and a short expiry. State the residual risk.
- Android only (iOS restricts card emulation).
- Rate risk: the rate lock covers the cheque window only; keep tap amounts small.
- ML-DSA covers account/transaction signatures only. TEE signatures from NEAR AI Cloud are still ed25519. Validators stay classical. Reference signers are not constant-time.
- Cash-out to a Nigerian bank needs a licensed partner; we do not custody or convert fiat ourselves.
- Nothing here has moved real money until a run says so.

## Mandates (idea 4)
The vault policy is the mandate. Add the fields traditional mandates have: `expires_at` (end date), per-payment cap, daily cap, minimum interval (frequency), payee scope (allow-list), revocation. Export a **Mandate Passport** JSON in an AP2/cVRP-inspired shape ("inspired", not compliant), signed by the owner's ML-DSA key and hashed into every receipt, so a bank, processor or auditor can read what the agent was allowed to do.

## Build order (commit after every green step)
| # | Step | Verify with |
|---|---|---|
| 0 | Baseline commit: secrets scan, `.gitignore`, current Python package, contract, docs | tests + scan |
| 1 | **Spikes** in the NEAR sandbox/testnet: (a) can a restricted function-call key sign a DelegateAction that calls `pay_direct`, with the vault as sender and a third account as relayer? Does `only_agent` see the delegate key? (b) can an ML-DSA-65 key be added as a function-call key on testnet and sign? Read NEP-645 for the exact borsh/key-type encoding | sandbox test, one testnet tx |
| 2 | ML-DSA-65 (FIPS 204) in pure Python; cross-check against a reference library and known-answer vectors; wire into `KeyPair`, tx and delegate encoding | vectors + testnet tx |
| 3 | Contract v2: `expires_at`, `min_interval`, `get_mandate` view; sandbox tests incl. delegate path; redeploy to testnet | cargo test + testnet txs |
| 4 | Quantum-safe vault on testnet: ML-DSA owner + agent keys, ed25519 full-access keys deleted, proof via access-key listing | testnet evidence in docs |
| 5 | Tap protocol library (Python): messages, canonical encoding, rate lock, cheque build/verify, simulator of two phones over loopback/QR text; shared test vectors | unit + e2e stage |
| 6 | Settlement + cash-out: delegate submission, `Rail` interface, `SandboxRail`, a real partner rail only if one is licensed and confirmed, Intents leg | unit + sandbox |
| 7 | Mandate Passport + receipt v2 + browser verifier page | unit + manual page check |
| 8 | Android: Kotlin `PaymentLink` (NFC HCE + reader, Nearby, QR), Bouncy Castle ML-DSA, biometric confirm, buyer/merchant screens; Gradle unit tests using the shared vectors | `./gradlew` tests; two-phone test is the user's |
| 9 | README/docs rewritten to match code, e2e suite stages, fresh-copy test, demo script | fresh clone run |

## Unknowns to resolve first (do not assume)
1. DelegateAction signed by a restricted function-call key for `pay_direct` (step 1a).
2. ML-DSA key wire format and function-call permission on testnet (step 1b); gas cost 100 Ggas per verification.
3. Android HCE throughput for ~4 KB (signature is 3,309 bytes); decide NFC-only vs NFC-bootstrap + Bluetooth/Wi-Fi after measuring on real phones.
4. Whether BouncyCastle's ML-DSA runs acceptably on the target phones.
5. A licensed payout partner (none is integrated).

## Reuse (already built, tested)
`vault.py`, `receipts.py`, `attestation.py`, `oneclick.py`, `swap.py`, `cost.py`, `near/*`, the contract (20 unit + 3 sandbox tests), the e2e suite.
