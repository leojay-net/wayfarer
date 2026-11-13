package com.wayfarer.app

import android.content.Context
import android.content.Intent
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.os.Bundle
import android.os.CancellationSignal
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.wayfarer.tap.AcceptedCheque
import com.wayfarer.tap.ApduClient
import com.wayfarer.tap.BuyerWallet
import com.wayfarer.tap.ChequeEnvelope
import com.wayfarer.tap.CommitResult
import com.wayfarer.tap.FrameAssembler
import com.wayfarer.tap.MerchantTerminal
import com.wayfarer.tap.PaymentRequest
import com.wayfarer.tap.SeedSigner
import com.wayfarer.tap.TapException
import com.wayfarer.tap.encodeFrames
import com.wayfarer.tap.sha256
import com.wayfarer.tap.toHex
import java.math.BigDecimal
import java.math.BigInteger

private val Ink = Color(0xFF1B1B1B)
private val Accent = Color(0xFF0B5FFF)

/**
 * Wayfarer Tap, prototype. TESTNET ONLY. Not audited. Not yet run on two real phones: NFC throughput and the QR
 * scan loop are untested on hardware (the protocol logic itself is unit-tested in wayfarer-tap).
 */
class MainActivity : ComponentActivity() {
    private lateinit var store: SeedStore
    private var nfc: NfcReader? = null

    // Shared UI state
    private var role by mutableStateOf<String?>(null)
    private var message by mutableStateOf("")

    // Merchant
    private var terminal: MerchantTerminal? = null
    private var request by mutableStateOf<PaymentRequest?>(null)
    private var accepted by mutableStateOf<AcceptedCheque?>(null)
    private val chequeAssembler = FrameAssembler()

    // Buyer
    private var incoming by mutableStateOf<PaymentRequest?>(null)
    private var envelope by mutableStateOf<ChequeEnvelope?>(null)
    private var sentResult by mutableStateOf<CommitResult?>(null)
    private val requestAssembler = FrameAssembler()

    private fun prefs() = getSharedPreferences("wayfarer-settings", Context.MODE_PRIVATE)

    private fun setting(key: String, default: String) = prefs().getString(key, default) ?: default

    private fun save(key: String, value: String) = prefs().edit().putString(key, value).apply()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = SeedStore(this)
        nfc = NfcReader(this) { client, _ -> onPhoneTouched(client) }
        setContent { Screen() }
    }

    override fun onResume() {
        super.onResume()
        if (role == "buyer") nfc?.start()
    }

    override fun onPause() {
        super.onPause()
        nfc?.stop()
    }

    // ------------------------------------------------------------------------------------------------ biometric

    private fun authenticate(title: String, onOk: () -> Unit) {
        val manager = getSystemService(BiometricManager::class.java)
        val allowed = BiometricManager.Authenticators.BIOMETRIC_STRONG or BiometricManager.Authenticators.DEVICE_CREDENTIAL
        if (manager.canAuthenticate(allowed) != BiometricManager.BIOMETRIC_SUCCESS) {
            message = "Set a screen lock or fingerprint on this phone first; the payment key needs it."
            return
        }
        val prompt = BiometricPrompt.Builder(this).setTitle(title).setAllowedAuthenticators(allowed).build()
        prompt.authenticate(CancellationSignal(), mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) = onOk()

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                message = "Not confirmed: $errString"
            }
        })
    }

    // -------------------------------------------------------------------------------------------------- merchant

    private fun startSale(nairaText: String) {
        try {
            val naira = BigDecimal(nairaText.trim())
            val minor = naira.movePointRight(2).longValueExact()
            val rate = BigDecimal(setting("rate", "5000"))
            val term = MerchantTerminal(
                account = setting("merchant_account", ""),
                name = setting("merchant_name", "My shop"),
                fiatPerToken = { rate },
                clockMs = System::currentTimeMillis,
            )
            terminal = term
            accepted = null
            val created = term.createRequest(minor)
            request = created
            MerchantCard.server.outgoing = created.message()
            MerchantCard.handler = { raw ->
                try {
                    val ok = term.accept(raw)
                    runOnUiThread { accepted = ok }
                    CommitResult(true, "Accepted, pending settlement")
                } catch (e: TapException) {
                    CommitResult(false, e.message ?: "Refused.")
                }
            }
            message = "Waiting for the buyer. Hold their phone to the back of this one, or let them scan the code."
        } catch (e: TapException) {
            message = e.message ?: "Could not create the request."
        } catch (e: ArithmeticException) {
            message = "Enter an amount in naira, at most two decimals."
        } catch (e: NumberFormatException) {
            message = "Enter an amount in naira."
        }
    }

    private fun scanChequeFrame() = scanOne(this, { text ->
        try {
            val progress = chequeAssembler.add(text)
            if (progress.complete) {
                accepted = terminal?.accept(chequeAssembler.payload())
                message = "Cheque received."
            } else {
                message = "Got ${progress.received} of ${progress.total} frames. Scan the next one."
            }
        } catch (e: TapException) {
            message = e.message ?: "Refused."
        }
    }, { message = it })

    private fun shareAccepted(a: AcceptedCheque) {
        val json = """{"note":"Wayfarer accepted cheque. PENDING SETTLEMENT. Settle with scripts/settle_cheque.py.","request_hex":"${a.request.message().toHex()}","envelope_hex":"${
            ChequeEnvelope(a.request.requestHash(), a.cheque.vault, a.keyId.chunked(2).map { it.toInt(16).toByte() }.toByteArray(), a.signature, a.publicKey).encode().toHex()
        }"}"""
        startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, json), "Share accepted cheque"))
    }

    // ---------------------------------------------------------------------------------------------------- buyer

    private fun onPhoneTouched(client: ApduClient) {
        try {
            val ready = envelope
            if (ready != null && incoming != null) {
                val result = client.sendCheque(ready.encode())
                runOnUiThread {
                    sentResult = result
                    message = if (result.accepted) "Sent. The merchant has it, pending settlement. It is NOT final until it settles on-chain." else "Refused: ${result.text}"
                }
            } else {
                val got = PaymentRequest.decode(client.fetchRequest())
                runOnUiThread { review(got) }
            }
        } catch (e: TapException) {
            runOnUiThread { message = e.message ?: "Tap failed." }
        } catch (e: java.io.IOException) {
            runOnUiThread { message = "The phones moved apart. Hold them together and try again." }
        }
    }

    private fun review(got: PaymentRequest) {
        incoming = got
        envelope = null
        sentResult = null
        message = "Check the amount, then approve."
    }

    private fun scanRequest() = scanOne(this, { text ->
        try {
            val progress = requestAssembler.add(text)
            if (progress.complete) review(PaymentRequest.decode(requestAssembler.payload()))
        } catch (e: TapException) {
            message = e.message ?: "Could not read that code."
        }
    }, { message = it })

    private fun approve(req: PaymentRequest) {
        authenticate("Pay ${req.fiatDisplay()} to ${req.merchantName}") {
            try {
                val seed = store.load()
                val limit = BigDecimal(setting("tap_limit_near", "0.5")).movePointRight(24).toBigIntegerExact()
                val wallet = BuyerWallet(setting("vault", ""), SeedSigner(seed), { true }, mapOf("near" to limit), System::currentTimeMillis)
                envelope = wallet.pay(req)
                seed.fill(0)
                message = "Approved and signed. Hold the phones together again to send it, or show the code."
            } catch (e: TapException) {
                message = e.message ?: "Could not sign."
            } catch (e: Exception) {
                message = "Could not use the key: ${e.message}"
            }
        }
    }

    // ------------------------------------------------------------------------------------------------------ UI

    @Composable
    private fun Screen() {
        Column(Modifier.fillMaxSize().background(Color.White).systemBarsPadding().verticalScroll(rememberScrollState()).padding(16.dp), Arrangement.spacedBy(10.dp)) {
            Title("Wayfarer Tap")
            Note("PROTOTYPE. TESTNET ONLY. UNAUDITED. Not tested on real phones yet. Rates are typed in by hand (DEMO), not a market feed.")
            when (role) {
                null -> {
                    Btn("I am the merchant") { role = "merchant"; nfc?.stop() }
                    Btn("I am the buyer") { role = "buyer"; nfc?.start() }
                }
                "merchant" -> MerchantScreen()
                else -> BuyerScreen()
            }
            if (message.isNotEmpty()) BasicText(message, style = TextStyle(color = Accent, fontSize = 15.sp))
            if (role != null) Btn("Back") { role = null; message = ""; nfc?.stop() }
        }
    }

    @Composable
    private fun MerchantScreen() {
        var account by remember { mutableStateOf(setting("merchant_account", "")) }
        var name by remember { mutableStateOf(setting("merchant_name", "")) }
        var rate by remember { mutableStateOf(setting("rate", "5000")) }
        var amount by remember { mutableStateOf("") }
        Title("Merchant")
        Field("Your NEAR account (e.g. shop.testnet)", account) { account = it; save("merchant_account", it) }
        Field("Shop name", name) { name = it; save("merchant_name", it) }
        Field("DEMO rate: naira per 1 NEAR", rate) { rate = it; save("rate", it) }
        Field("Amount in naira", amount) { amount = it }
        Btn("Create payment request") { startSale(amount) }
        request?.let { r ->
            Title(r.fiatDisplay() + " = " + com.wayfarer.tap.assetText(r.tokenId, r.assetAmount))
            Note("Price locked for 60 seconds at a DEMO rate of ${r.rate}.")
            FrameImages(encodeFrames(r.message(), r.nonce.copyOf(4)))
        }
        accepted?.let { a ->
            Title("ACCEPTED, PENDING SETTLEMENT")
            Note("Not paid yet. The signature checks out offline, but the payment can still fail (no funds, key revoked, expired). It is final only after it settles on-chain.")
            Btn("Share the accepted cheque for settlement") { shareAccepted(a) }
        }
        if (request != null && accepted == null) Btn("Scan the buyer's cheque code (QR fallback)") { scanChequeFrame() }
    }

    @Composable
    private fun BuyerScreen() {
        var vault by remember { mutableStateOf(setting("vault", "")) }
        var limit by remember { mutableStateOf(setting("tap_limit_near", "0.5")) }
        Title("Buyer")
        Field("Your vault account (e.g. my-vault.testnet)", vault) { vault = it; save("vault", it) }
        Field("Tap limit in NEAR", limit) { limit = it; save("tap_limit_near", it) }
        if (!store.exists) {
            Btn("Create my payment key") { authenticate("Create your payment key") { store.createIfMissing(); message = "Key created. Share its public key with the vault owner to register it." } }
        } else {
            Btn("Share my public key (for the vault owner)") { sharePublicKey() }
        }
        if (nfc?.available == false) Note("NFC is off or missing; use the QR option.")
        val req = incoming
        if (req == null) {
            Note("Hold this phone to the back of the merchant's phone, or scan their code.")
            Btn("Scan the merchant's code (QR)") { scanRequest() }
        } else {
            Title("Pay " + req.fiatDisplay() + " to " + req.merchantName)
            Note("= ${com.wayfarer.tap.assetText(req.tokenId, req.assetAmount)} from your vault. Merchant account: ${req.merchantAccount}")
            val env = envelope
            if (env == null) {
                Btn("Approve with fingerprint or PIN") { approve(req) }
                Btn("Cancel") { incoming = null; message = "" }
            } else if (sentResult == null) {
                Note("Signed. NOT SENT YET. Hold the phones together again, or let the merchant scan this:")
                FrameImages(encodeFrames(env.encode(), req.nonce.copyOf(4)))
            }
        }
    }

    private fun sharePublicKey() {
        val seed = ByteArray(0) // the public key is derived on demand; unlocking is required to read the seed
        authenticate("Show your public key") {
            val s = store.load()
            val publicKey = SeedSigner(s).publicKey
            s.fill(0)
            val text = "Wayfarer buyer key (ML-DSA-65)\nkey_id: ${sha256(publicKey).toHex()}\npublic_key_hex: ${publicKey.toHex()}"
            startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text), "Share public key"))
        }
        seed.fill(0)
    }

    @Composable
    private fun FrameImages(frames: List<String>) {
        var index by remember(frames) { mutableStateOf(0) }
        LaunchedEffect(frames) {
            while (true) {
                kotlinx.coroutines.delay(900)
                index = (index + 1) % frames.size
            }
        }
        val bitmap = remember(frames, index) { qrBitmap(frames[index]).asImageBitmap() }
        Image(bitmap, "QR frame ${index + 1} of ${frames.size}", Modifier.fillMaxWidth().size(320.dp))
        Note("Frame ${index + 1} of ${frames.size}. Keep it in view until every frame has been scanned.")
    }

    @Composable
    private fun Title(text: String) = BasicText(text, style = TextStyle(color = Ink, fontSize = 20.sp, fontWeight = FontWeight.SemiBold))

    @Composable
    private fun Note(text: String) = BasicText(text, style = TextStyle(color = Color(0xFF555555), fontSize = 14.sp))

    @Composable
    private fun Btn(text: String, onClick: () -> Unit) = Box(Modifier.fillMaxWidth().background(Accent).clickable(onClick = onClick).padding(14.dp)) {
        BasicText(text, style = TextStyle(color = Color.White, fontSize = 16.sp))
    }

    @Composable
    private fun Field(label: String, value: String, onChange: (String) -> Unit) {
        Note(label)
        Box(Modifier.fillMaxWidth().background(Color(0xFFEFEFEF)).padding(10.dp)) {
            BasicTextField(value, onChange, textStyle = TextStyle(color = Ink, fontSize = 16.sp), singleLine = true)
        }
    }
}
