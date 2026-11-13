# Demo video (about 91 s)

`wayfarer-demo.mp4` is built from real recordings and real output. Narration: Vultr Serverless Inference text to speech (`qwen3-tts-12hz-1.7b`, designed "Nigerian man" voice, fixed seed 7). Captions are burned in and also shipped as `wayfarer-demo.srt`.

What is real in it: the Android app screens recorded on the Android 36 emulator (merchant keypad, request QR, buyer review, PIN approval, ML-DSA-65 signing, 6-frame cheque QR); `scripts/tap_demo.py --naira 1500 --testnet 9bda7c` (a real settlement on NEAR testnet); and the verifier page in headless Chrome (20 of 20 checks, offline and against the testnet RPC).

What is not: the two phones are simulated in one process for the settlement step; the buyer's request is injected into the emulator by a debug-only intent (no second phone, no camera); the PIN screen is Android's secure screen and cannot be recorded, so the video labels it; the rate is typed in; the cash-out is a sandbox; the NFC tap has not been tested on two real phones; the code is unaudited.

| Scene | Narration |
|---|---|
| Title | This is Wayfarer. Spend naira. Hold NEAR. |
| Merchant | A shop charges in naira. The merchant types the price on a keypad. The app locks a demo exchange rate for sixty seconds, and shows a request code. |
| Buyer | The buyer sees exactly what they are paying: the amount in naira, the amount in NEAR, and the vault it comes from. They approve with a fingerprint or PIN. The phone then signs a cheque with a post-quantum signature, M L D S A sixty five. The signed cheque is too big for one code, so it travels across in six frames. |
| Settlement | The merchant checks the signature offline, and accepts it, pending settlement. Then the cheque settles through the vault on NEAR testnet. The contract verifies the signature on chain, and pays. |
| Verifier | Anyone can verify the receipt in a browser. Twenty checks, offline and against the chain, all pass. |
| Honest status | To be clear about what is real. The signing, the cheque, the testnet settlement and the verifier are real. The tap itself has not been tested on two real phones. The exchange rate is a demo, the cash out is a sandbox, and the code is unaudited. |
| End | Wayfarer. Built at the NEAR hackathon. |

If you later run the two-phone test (`docs/TWO-PHONE-TEST.md`), record that and replace the emulator scenes.
