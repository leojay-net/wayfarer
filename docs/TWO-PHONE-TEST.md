# Two-phone test and demo (Android only)

This has **not been done yet**. It is the one check that turns "simulated phones" into a real tap. Everything else in this guide was run, except the two phones.

## Why both phones must be Android
- The merchant phone acts as a card (NFC host card emulation). iOS does not let third-party apps do that.
- Both apps are Android apps (`wayfarer-app`, minSdk 30 = Android 11+). There is no iOS app.
- Each phone needs NFC, a screen lock (fingerprint or PIN; the payment key will not work without it) and Google Play services (only for the QR fallback scanner).
- Two emulators cannot test the tap: they have no NFC between them. One emulator was used only to check that the app launches.

## What you need
Two Android 11+ phones with NFC, a laptop with this repo, the testnet accounts of `docs/TESTNET-PQ.md` and their seeds in `~/.near-credentials/wayfarer-pq/`, and a way to send text from a phone to the laptop (email, chat, USB). Testnet only: nothing here has value. The vault `wfpq-vault-9bda7c.testnet` held 9.5 testnet NEAR on 13 Nov.

## Steps
1. **Install on both phones** (USB debugging on): `./gradlew :wayfarer-app:assembleDebug`, then `adb install -r wayfarer-app/build/outputs/apk/debug/wayfarer-app-debug.apk` (use `adb -s <serial>` for each phone).
2. **Buyer phone:** open the app, **I am the buyer**. Set the vault to `wfpq-vault-9bda7c.testnet` and the tap limit (0.5 NEAR). **Create my payment key** (fingerprint/PIN). **Share my public key**, and send the text to the laptop; it contains `key_id` and `public_key_hex`.
3. **Laptop:** register that key on the vault: `python3 scripts/add_phone_key.py --suffix 9bda7c --public-key-hex <public_key_hex> --hours 24`.
4. **Merchant phone:** **I am the merchant**. Account `wfpq-shop-9bda7c.testnet` (the relayer key in step 6 belongs to this account), a shop name, DEMO rate `5000`, amount `500`. **Create payment request.** Keep the screen on and unlocked.
5. **Tap.** Buyer app open on the buyer screen, unlocked. Touch the backs of the phones (NFC antennas are usually near the camera; move slowly). The buyer shows "Pay NGN 500.00 to ... = 0.100000 NEAR". **Approve** (fingerprint/PIN), then touch again to send. The merchant shows **ACCEPTED, PENDING SETTLEMENT**, which is not payment. If NFC fails, use the QR path (**Scan the merchant's code**, then show the cheque frames to the merchant's scanner).
6. **Settle.** On the merchant phone press **Share the accepted cheque** and send it to the laptop as `accepted.json`. Then:
   `python3 scripts/settle_cheque.py accepted.json --check-only`
   `python3 scripts/settle_cheque.py accepted.json --suffix 9bda7c --save out/ --revoke`
   This submits `pay_with_cheque` (the shop account pays gas), prints the explorer link, runs the 20 receipt checks, saves `out/receipt.json` and revokes the phone's key.
7. **Receipt.** `python3 -m http.server -d verifier 8000`, open `http://localhost:8000`, paste `out/receipt.json`, **Check against the chain**.

## Write down (these are the open unknowns)
- Seconds from first touch to "Approve" appearing, and from "Approve" to "ACCEPTED". A 5.4 KB cheque is about 23 NFC writes of 240 bytes.
- How many touches or retries it took, on which two phone models, and whether the QR fallback worked.
- How long signing took on the phone (watch for a visible pause after fingerprint).
- Anything that crashed. `adb logcat -s AndroidRuntime:E` on each phone shows it.
Put the numbers in the README only after you have them.

## If you only have one Android phone
The app needs two phones for a tap (one is the merchant card, one is the reader). With one phone, show the simulated demo instead (`python3 scripts/tap_demo.py --naira 500 --testnet 9bda7c`) and say plainly that the phones are simulated.
