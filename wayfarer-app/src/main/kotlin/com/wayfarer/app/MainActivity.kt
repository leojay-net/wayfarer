package com.wayfarer.app

import android.content.Context
import android.content.Intent
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.os.Bundle
import android.os.CancellationSignal
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.core.animateFloat
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.ui.draw.scale
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.wayfarer.tap.AcceptedCheque
import com.wayfarer.tap.ApduClient
import com.wayfarer.tap.BuyerWallet
import com.wayfarer.tap.ChequeEnvelope
import com.wayfarer.tap.CommitResult
import com.wayfarer.tap.FrameAssembler
import com.wayfarer.tap.MerchantTerminal
import com.wayfarer.tap.PaymentRequest
import com.wayfarer.tap.PricedOrder
import com.wayfarer.tap.SeedSigner
import com.wayfarer.tap.TapException
import com.wayfarer.tap.encodeFrames
import com.wayfarer.tap.sha256
import com.wayfarer.tap.toHex
import java.math.BigDecimal
import java.math.BigInteger

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
    private var keyReady by mutableStateOf(false)

    // Merchant
    private var terminal: MerchantTerminal? = null
    private var request by mutableStateOf<PaymentRequest?>(null)
    private var accepted by mutableStateOf<AcceptedCheque?>(null)
    private val chequeAssembler = FrameAssembler()
    private var agentMode by mutableStateOf(false)
    private var agentBusy by mutableStateOf(false)
    private var agentResult by mutableStateOf<PricedOrder?>(null)

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
        keyReady = store.exists
        nfc = NfcReader(this) { client, _ -> onPhoneTouched(client) }
        setContent { Screen() }
        handleDemoIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDemoIntent(intent)
    }

    /**
     * Debug builds only: `adb shell am start ... --es demo_request_hex <hex>` hands the buyer a payment request as if it
     * had just been scanned, so the buyer screens can be driven on a single emulator. Release builds ignore it.
     */
    private fun handleDemoIntent(intent: Intent?) {
        val debuggable = applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0
        val hex = intent?.getStringExtra("demo_request_hex") ?: return
        if (!debuggable) return
        try {
            role = "buyer"
            // "auto" takes the request the merchant side of this same app just created (single-emulator demo).
            val raw = if (hex == "auto") MerchantCard.server.outgoing ?: throw IllegalStateException("no request yet")
            else hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
            review(PaymentRequest.decode(raw))
        } catch (e: Exception) {
            message = "Could not read the demo request."
        }
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
            message = ""
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
        message = ""
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
                message = ""
            } catch (e: TapException) {
                message = e.message ?: "Could not sign."
            } catch (e: Exception) {
                message = "Could not use the key: ${e.message}"
            }
        }
    }

    // ------------------------------------------------------------------------------------------------------ UI

    private fun tone(text: String) = when {
        text.startsWith("Not confirmed") || text.startsWith("Refused") || text.startsWith("Could not") ||
            text.startsWith("Enter") || text.startsWith("Set a screen") || text.contains("moved apart") -> Tone.Bad
        else -> Tone.Neutral
    }

    /** Name of the screen on show; logged in debug builds so the demo recording script can wait for it. */
    private fun screenName(): String = when (role) {
        null -> "home"
        "merchant" -> if (accepted != null) "merchant_accepted" else if (request != null) "merchant_request" else if (agentMode) "merchant_agent" else "merchant_keypad"
        else -> if (sentResult?.accepted == true) "buyer_sent" else if (envelope != null) "buyer_signed" else if (incoming != null) "buyer_review" else "buyer_radar"
    }

    @Composable
    private fun Screen() {
        val name = screenName()
        LaunchedEffect(name) {
            if (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0) android.util.Log.d("WF_STATE", name)
        }
        Page {
            Column(Modifier.fillMaxSize().systemBarsPadding().padding(horizontal = 22.dp)) {
                Box(Modifier.weight(1f)) {
                    androidx.compose.animation.AnimatedContent(
                        targetState = role to (request?.let { 1 } ?: 0) + (accepted?.let { 2 } ?: 0) + (incoming?.let { 4 } ?: 0) + (envelope?.let { 8 } ?: 0) + (sentResult?.let { 16 } ?: 0),
                        transitionSpec = {
                            (androidx.compose.animation.fadeIn(androidx.compose.animation.core.tween(380, 120)) +
                                androidx.compose.animation.slideInVertically(androidx.compose.animation.core.tween(480, 80, androidx.compose.animation.core.FastOutSlowInEasing)) { it / 14 } +
                                androidx.compose.animation.scaleIn(androidx.compose.animation.core.tween(480, 80), 0.97f))
                                .togetherWith(androidx.compose.animation.fadeOut(androidx.compose.animation.core.tween(160)))
                        },
                        label = "screen",
                    ) { (r, _) ->
                        when (r) {
                            null -> HomeScreen()
                            "merchant" -> MerchantScreen()
                            else -> BuyerScreen()
                        }
                    }
                }
                if (message.isNotEmpty()) {
                    Banner(message, tone(message)) { message = "" }
                    Spacer(Modifier.height(10.dp))
                }
                TestnetStrip()
                Spacer(Modifier.height(10.dp))
            }
        }
    }

    private fun leave() { agentMode = false; agentResult = null; agentBusy = false; if (role != "buyer") accepted = null; role = null; message = ""; request = null; incoming = null; envelope = null; sentResult = null; nfc?.stop() }

    @Composable
    private fun HomeScreen() {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), Arrangement.spacedBy(0.dp)) {
            Spacer(Modifier.height(20.dp))
            Box(Modifier.appear(0)) { Wordmark() }
            Spacer(Modifier.height(30.dp))
            Box(Modifier.appear(1)) { Eyebrow("Quantum-safe tap to pay", Wf.Mint) }
            Spacer(Modifier.height(12.dp))
            Box(Modifier.appear(2)) { T("Spend naira.\nHold NEAR.", 46, FontWeight.Light, spacing = -1.6, lineHeight = 50) }
            Spacer(Modifier.height(26.dp))
            Box(Modifier.appear(3)) { VaultCard(setting("vault", "wfpq-vault-9bda7c.testnet"), setting("tap_limit_near", "0.5")) }
            Spacer(Modifier.height(18.dp))
            Box(Modifier.appear(4)) { RoleCard(true, "Pay", "Approve a price and settle from your vault", "Buyer") { role = "buyer"; nfc?.start() } }
            Spacer(Modifier.height(12.dp))
            Box(Modifier.appear(5)) { RoleCard(false, "Accept payments", "Charge in naira, get a signed cheque", "Merchant") { role = "merchant"; nfc?.stop() } }
            Spacer(Modifier.height(24.dp))
        }
    }

    @Composable
    private fun RoleCard(buyer: Boolean, title: String, subtitle: String, tag: String, onClick: () -> Unit) {
        val source = remember { androidx.compose.foundation.interaction.MutableInteractionSource() }
        val pressed by source.collectIsPressedAsState()
        val sc by animateFloatAsState(if (pressed) 0.97f else 1f, spring(Spring.DampingRatioMediumBouncy, Spring.StiffnessMedium), label = "role")
        Card(Modifier.scale(sc).androidxClickable(source, onClick), pad = 18.dp) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconTile { RoleIcon(buyer) }
                Spacer(Modifier.width(16.dp))
                Column(Modifier.weight(1f)) {
                    Eyebrow(tag)
                    Spacer(Modifier.height(4.dp))
                    T(title, 21, FontWeight.Medium, spacing = -0.4)
                    Spacer(Modifier.height(2.dp))
                    T(subtitle, 13, color = Wf.Dim, lineHeight = 18)
                }
                Chevron()
            }
        }
    }

    private fun Modifier.androidxClickable(source: androidx.compose.foundation.interaction.MutableInteractionSource, onClick: () -> Unit) =
        this.then(Modifier.clickable(interactionSource = source, indication = null, onClick = onClick))

    @Composable
    private fun Chevron() {
        androidx.compose.foundation.Canvas(Modifier.size(18.dp)) {
            val p = androidx.compose.ui.graphics.Path().apply {
                moveTo(size.width * 0.32f, size.height * 0.1f); lineTo(size.width * 0.72f, size.height * 0.5f); lineTo(size.width * 0.32f, size.height * 0.9f)
            }
            drawPath(p, Wf.Dim, style = androidx.compose.ui.graphics.drawscope.Stroke(2.dp.toPx(), cap = androidx.compose.ui.graphics.StrokeCap.Round, join = androidx.compose.ui.graphics.StrokeJoin.Round))
        }
    }

    // ---- merchant

    @Composable
    private fun MerchantScreen() {
        var account by remember { mutableStateOf(setting("merchant_account", "")) }
        var name by remember { mutableStateOf(setting("merchant_name", "")) }
        var rate by remember { mutableStateOf(setting("rate", "5000")) }
        var amount by remember { mutableStateOf("") }
        var settings by remember { mutableStateOf(account.isEmpty()) }
        val req = request
        val got = accepted
        Column(Modifier.fillMaxSize()) {
            TopBar(
                if (got != null) "Accepted" else if (req != null) "Waiting for buyer" else "Charge",
                onBack = { if (req != null && got == null) { request = null; message = "" } else leave() },
            ) {
                if (req == null) TextAction(if (settings) "Done" else "Setup", Wf.Mint) { settings = !settings }
            }
            when {
                got != null -> AcceptedPanel(got)
                req != null -> RequestPanel(req)
                settings -> Column(Modifier.weight(1f).verticalScroll(rememberScrollState()), Arrangement.spacedBy(18.dp)) {
                    Spacer(Modifier.height(8.dp))
                    T("Terminal", 28, FontWeight.Light, spacing = -0.6)
                    T("Where the money lands, and the demo exchange rate. Nothing here is a live market feed.", 14, color = Wf.Dim, lineHeight = 20)
                    Field("Your NEAR account", account, mono = true) { account = it; save("merchant_account", it) }
                    Field("Shop name", name) { name = it; save("merchant_name", it) }
                    Field("Demo rate (naira per 1 NEAR)", rate, keyboard = KeyboardType.Decimal) { rate = it; save("rate", it) }
                    T("Order agent. Optional. Your own NEAR AI Cloud key; only the order text is sent, never buyer data.", 13, color = Wf.Dim, lineHeight = 18)
                    Field("NEAR AI key", setting("nearai_key", ""), mono = true) { save("nearai_key", it) }
                }
                agentMode -> Column(Modifier.weight(1f)) {
                    ModeToggle()
                    AgentPanel()
                }
                else -> Column(Modifier.weight(1f)) {
                    Spacer(Modifier.height(4.dp))
                    ModeToggle()
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                        Pill(name.ifEmpty { "Your shop" }, Tone.Neutral)
                    }
                    Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Amount("₦", groupEntry(amount), 60, dim = amount.isEmpty())
                            Spacer(Modifier.height(10.dp))
                            T(nearHint(amount, rate), 14, color = Wf.Dim)
                        }
                    }
                    Keypad { key -> amount = editAmount(amount, key) }
                    Spacer(Modifier.height(14.dp))
                    PrimaryButton("Charge", enabled = amount.isNotEmpty() && amount != ".") { startSale(amount) }
                    Spacer(Modifier.height(14.dp))
                }
            }
        }
    }

    @Composable
    private fun ModeToggle() {
        Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            TextAction("Keypad", if (!agentMode) Wf.Mint else Wf.Dim) { agentMode = false }
            Spacer(Modifier.width(26.dp))
            TextAction("Order agent", if (agentMode) Wf.Mint else Wf.Dim) { agentMode = true }
        }
    }

    private fun runAgent(order: String) {
        agentBusy = true; agentResult = null; message = ""
        val key = setting("nearai_key", "")
        Thread {
            try {
                val r = MerchantAgent.price(order, MerchantAgent.DEMO_MENU, key)
                runOnUiThread { agentResult = r; agentBusy = false }
            } catch (e: MerchantAgent.AgentException) {
                runOnUiThread { message = e.message ?: "The agent could not price that order."; agentBusy = false }
            } catch (e: Exception) {
                runOnUiThread { message = "The agent could not price that order."; agentBusy = false }
            }
        }.start()
    }

    @Composable
    private fun androidx.compose.foundation.layout.ColumnScope.AgentPanel() {
        var order by remember { mutableStateOf("") }
        val res = agentResult
        Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState()), Arrangement.spacedBy(14.dp)) {
            T("Say what they want", 26, FontWeight.Light, spacing = -0.6)
            T("The agent picks items from your menu. Prices always come from the menu, never from the agent.", 13, color = Wf.Dim, lineHeight = 18)
            Card(Modifier.appear(1), pad = 14.dp) {
                Column {
                    MerchantAgent.DEMO_MENU.forEachIndexed { i, m ->
                        if (i > 0) Hairline()
                        DetailRow(m.name, "₦" + groupEntry(m.priceNaira.toString()))
                    }
                }
            }
            Field("Order", order) { order = it; agentResult = null }
            PrimaryButton(if (agentBusy) "Reading the order..." else "Price the order", enabled = order.isNotBlank() && !agentBusy) { runAgent(order) }
            if (res != null) {
                Card(Modifier.appear(2), pad = 14.dp) {
                    Column {
                        res.lines.forEachIndexed { i, l ->
                            if (i > 0) Hairline()
                            DetailRow("${l.quantity} x ${l.item.name}", "₦" + groupEntry(l.subtotalNaira.toString()))
                        }
                        if (res.unmatched.isNotEmpty()) {
                            Spacer(Modifier.height(8.dp))
                            Pill("Not on the menu: " + res.unmatched.joinToString(", "), Tone.Bad)
                        }
                    }
                }
                if (res.ready) {
                    Amount("₦", groupEntry(res.totalNaira.toString()), 44)
                    PrimaryButton("Charge ₦" + groupEntry(res.totalNaira.toString())) { val t = res.totalNaira; agentResult = null; startSale(t.toString()) }
                } else {
                    T("Fix the order, or use the keypad.", 13, color = Wf.Dim)
                }
            }
            Spacer(Modifier.height(10.dp))
        }
    }

    @Composable
    private fun RequestPanel(r: PaymentRequest) {
        var now by remember { mutableStateOf(System.currentTimeMillis()) }
        LaunchedEffect(r) { while (true) { now = System.currentTimeMillis(); kotlinx.coroutines.delay(500) } }
        val left = ((r.quoteExpiresMs - now) / 1000).coerceAtLeast(0)
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(4.dp))
            Amount(symbolOf(r.fiatCurrency), figureOf(r), 54)
            Spacer(Modifier.height(8.dp))
            T("= " + nearText(r), 16, color = Wf.Dim)
            Spacer(Modifier.height(14.dp))
            Pill(if (left > 0) "Price locked  ${left / 60}:${(left % 60).toString().padStart(2, '0')}" else "Price expired", if (left > 0) Tone.Live else Tone.Warn)
            Spacer(Modifier.height(22.dp))
            val frames = remember(r) { encodeFrames(r.message(), r.nonce.copyOf(4)) }
            Frames(frames) { img, i -> QrCard(img, if (frames.size > 1) "Frame ${i + 1} of ${frames.size}. Keep it in view until the buyer has scanned all." else "Buyer: scan this, or hold phones together.") }
            Spacer(Modifier.height(20.dp))
            Card(pad = 4.dp) {
                Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                    PulseDot()
                    Column(Modifier.weight(1f)) {
                        T("Waiting for the buyer", 15, FontWeight.Medium)
                        T("Hold their phone to the back of this one, or let them scan the code.", 13, color = Wf.Dim, lineHeight = 18)
                    }
                }
            }
            Spacer(Modifier.height(12.dp))
            SecondaryButton("Scan the buyer's cheque code") { scanChequeFrame() }
            Spacer(Modifier.height(14.dp))
        }
    }

    @Composable
    private fun AcceptedPanel(a: AcceptedCheque) {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(20.dp))
            SuccessMark()
            Spacer(Modifier.height(22.dp))
            T("Cheque accepted", 30, FontWeight.Light, spacing = -0.7)
            Spacer(Modifier.height(10.dp))
            Pill("Pending settlement", Tone.Warn)
            Spacer(Modifier.height(24.dp))
            Card(Modifier.appear(2), pad = 8.dp) {
                Column(Modifier.padding(horizontal = 12.dp)) {
                    DetailRow("Amount", figureWithSymbol(a.request))
                    Hairline()
                    DetailRow("In NEAR", nearText(a.request))
                    Hairline()
                    DetailRow("From vault", a.cheque.vault, mono = true)
                    Hairline()
                    DetailRow("Signature", "ML-DSA-65 verified", mono = false)
                }
            }
            Spacer(Modifier.height(16.dp))
            T(
                "Not paid yet. The signature checks out offline, but the payment can still fail: no funds, key revoked or expired. It is final only after it settles on-chain.",
                13, color = Wf.Dim, lineHeight = 19,
            )
            Spacer(Modifier.height(22.dp))
            PrimaryButton("Share cheque for settlement") { shareAccepted(a) }
            Spacer(Modifier.height(10.dp))
            SecondaryButton("New sale") { request = null; accepted = null; message = "" }
            Spacer(Modifier.height(14.dp))
        }
    }

    // ---- buyer

    @Composable
    private fun BuyerScreen() {
        var vault by remember { mutableStateOf(setting("vault", "")) }
        var limit by remember { mutableStateOf(setting("tap_limit_near", "0.5")) }
        var settings by remember { mutableStateOf(vault.isEmpty()) }
        val req = incoming
        val env = envelope
        val sent = sentResult
        Column(Modifier.fillMaxSize()) {
            TopBar(
                when {
                    sent?.accepted == true -> "Sent"
                    env != null -> "Signed"
                    req != null -> "Review"
                    else -> "Pay"
                },
                onBack = { if (req != null && sent == null) { incoming = null; envelope = null; message = "" } else leave() },
            ) { if (req == null) TextAction(if (settings) "Done" else "Wallet", Wf.Mint) { settings = !settings } }
            when {
                sent?.accepted == true && req != null -> SentPanel(req)
                env != null && req != null -> SignedPanel(req, env)
                req != null -> ReviewPanel(req, vault, limit)
                settings -> Column(Modifier.weight(1f).verticalScroll(rememberScrollState()), Arrangement.spacedBy(18.dp)) {
                    Spacer(Modifier.height(8.dp))
                    T("Wallet", 28, FontWeight.Light, spacing = -0.6)
                    T("Your payment key lives in the phone's secure hardware and signs only after you confirm.", 14, color = Wf.Dim, lineHeight = 20)
                    Field("Your vault account", vault, mono = true) { vault = it; save("vault", it) }
                    Field("Tap limit (NEAR)", limit, keyboard = KeyboardType.Decimal) { limit = it; save("tap_limit_near", it) }
                    Card {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                T("Payment key", 15, FontWeight.Medium)
                                T("ML-DSA-65, post-quantum", 13, color = Wf.Dim)
                            }
                            Pill(if (keyReady) "Created" else "Not created", if (keyReady) Tone.Live else Tone.Warn)
                        }
                        Spacer(Modifier.height(16.dp))
                        if (!keyReady) {
                            PrimaryButton("Create payment key") { authenticate("Create your payment key") { store.createIfMissing(); keyReady = true; message = "Key created. Share its public key with the vault owner to register it." } }
                        } else {
                            SecondaryButton("Share public key") { sharePublicKey() }
                        }
                    }
                }
                else -> Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                    Spacer(Modifier.weight(1f))
                    Radar()
                    Spacer(Modifier.height(32.dp))
                    T("Ready to pay", 32, FontWeight.Light, spacing = -0.8)
                    Spacer(Modifier.height(10.dp))
                    T(
                        if (nfc?.available == false) "NFC is off or missing on this phone. Scan the merchant's code instead."
                        else "Hold this phone to the back of the merchant's phone.",
                        15, color = Wf.Dim, align = TextAlign.Center, lineHeight = 22,
                    )
                    Spacer(Modifier.weight(1f))
                    SecondaryButton("Scan the merchant's code") { scanRequest() }
                    Spacer(Modifier.height(14.dp))
                }
            }
        }
    }

    @Composable
    private fun ReviewPanel(r: PaymentRequest, vault: String, limit: String) {
        Column(Modifier.fillMaxSize()) {
            Column(Modifier.weight(1f).verticalScroll(rememberScrollState()), horizontalAlignment = Alignment.CenterHorizontally) {
                Spacer(Modifier.height(10.dp))
                Eyebrow("Pay " + r.merchantName)
                Spacer(Modifier.height(16.dp))
                Amount(symbolOf(r.fiatCurrency), figureOf(r), 60)
                Spacer(Modifier.height(10.dp))
                T("= " + nearText(r), 17, color = Wf.Dim)
                Spacer(Modifier.height(28.dp))
                Card(Modifier.appear(2), pad = 8.dp) {
                    Column(Modifier.padding(horizontal = 12.dp)) {
                        DetailRow("To", r.merchantAccount, mono = true)
                        Hairline()
                        DetailRow("From vault", vault.ifEmpty { "not set" }, mono = true)
                        Hairline()
                        DetailRow("Tap limit", "$limit NEAR")
                        Hairline()
                        DetailRow("Rate (demo)", "${r.rate} per NEAR")
                    }
                }
            }
            PrimaryButton("Approve with fingerprint or PIN") { approve(r) }
            Spacer(Modifier.height(10.dp))
            Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) { TextAction("Decline", Wf.Dim) { incoming = null; message = "" } }
            Spacer(Modifier.height(4.dp))
        }
    }

    @Composable
    private fun SignedPanel(r: PaymentRequest, env: ChequeEnvelope) {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(6.dp))
            Pill("Signed. Not sent yet", Tone.Warn)
            Spacer(Modifier.height(14.dp))
            Amount(symbolOf(r.fiatCurrency), figureOf(r), 44)
            Spacer(Modifier.height(20.dp))
            val frames = remember(env) { encodeFrames(env.encode(), r.nonce.copyOf(4)) }
            Frames(frames) { img, i -> QrCard(img, if (frames.size > 1) "Frame ${i + 1} of ${frames.size}. Keep it in view until the merchant has scanned all." else "Merchant: scan this.") }
            Spacer(Modifier.height(18.dp))
            T("Hold the phones together again to send it by tap, or let the merchant scan the code.", 14, color = Wf.Dim, align = TextAlign.Center, lineHeight = 20)
            if (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0) {
                Spacer(Modifier.height(18.dp))
                SecondaryButton("Emulator only: deliver to merchant") {
                    // Same handler the NFC path calls, so the merchant still verifies the real ML-DSA-65 signature.
                    val result = MerchantCard.handler(env.encode())
                    sentResult = result
                    message = if (result.accepted) "" else "Refused: ${result.text}"
                }
            }
            Spacer(Modifier.height(14.dp))
        }
    }

    @Composable
    private fun SentPanel(r: PaymentRequest) {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(24.dp))
            SuccessMark()
            Spacer(Modifier.height(22.dp))
            T("Sent to " + r.merchantName, 28, FontWeight.Light, spacing = -0.6, align = TextAlign.Center)
            Spacer(Modifier.height(12.dp))
            Amount(symbolOf(r.fiatCurrency), figureOf(r), 40)
            Spacer(Modifier.height(14.dp))
            Pill("Pending settlement", Tone.Warn)
            Spacer(Modifier.height(20.dp))
            T("The merchant has your signed cheque. It is not final until it settles on-chain.", 14, color = Wf.Dim, align = TextAlign.Center, lineHeight = 20)
            Spacer(Modifier.weight(1f))
            PrimaryButton("Done") { incoming = null; envelope = null; sentResult = null; message = "" }
            Spacer(Modifier.height(14.dp))
        }
    }

    @Composable
    private fun PulseDot() {
        val t = androidx.compose.animation.core.rememberInfiniteTransition(label = "pulse")
        val a by t.animateFloat(
            0.25f, 1f,
            androidx.compose.animation.core.infiniteRepeatable(androidx.compose.animation.core.tween(900), androidx.compose.animation.core.RepeatMode.Reverse),
            label = "a",
        )
        Box(Modifier.size(12.dp).alpha(a).clip(androidx.compose.foundation.shape.CircleShape).background(Wf.Mint))
    }

    private fun sharePublicKey() {
        authenticate("Show your public key") {
            val s = store.load()
            val publicKey = SeedSigner(s).publicKey
            s.fill(0)
            val text = "Wayfarer buyer key (ML-DSA-65)\nkey_id: ${sha256(publicKey).toHex()}\npublic_key_hex: ${publicKey.toHex()}"
            startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text), "Share public key"))
        }
    }

    /** Cycles through multi-frame QR codes. */
    @Composable
    private fun Frames(frames: List<String>, render: @Composable (androidx.compose.ui.graphics.ImageBitmap, Int) -> Unit) {
        var index by remember(frames) { mutableStateOf(0) }
        LaunchedEffect(frames) {
            while (true) {
                kotlinx.coroutines.delay(900)
                index = (index + 1) % frames.size
            }
        }
        val bitmap = remember(frames, index) { qrBitmap(frames[index]).asImageBitmap() }
        render(bitmap, index)
    }

    // ---- formatting

    private fun symbolOf(currency: String) = if (currency == "NGN") "₦" else currency

    private fun figureOf(r: PaymentRequest): String =
        "%,.2f".format(BigDecimal(r.fiatMinor).movePointLeft(r.fiatExponent)).let { if (it.endsWith(".00")) it.dropLast(3) else it }

    private fun nearText(r: PaymentRequest): String =
        if (r.tokenId == "near") BigDecimal(r.assetAmount).movePointLeft(24).stripTrailingZeros().toPlainString() + " NEAR"
        else com.wayfarer.tap.assetText(r.tokenId, r.assetAmount)

    private fun figureWithSymbol(r: PaymentRequest) = symbolOf(r.fiatCurrency) + figureOf(r)

    /** Keypad entry: digits plus at most one point and two decimals, no leading zeros. */
    private fun editAmount(current: String, key: String): String = when (key) {
        "<" -> current.dropLast(1)
        "." -> if (current.contains('.')) current else (if (current.isEmpty()) "0." else "$current.")
        else -> when {
            current.contains('.') && current.substringAfter('.').length >= 2 -> current
            current == "0" -> key
            current.length >= 9 -> current
            else -> current + key
        }
    }

    private fun groupEntry(raw: String): String {
        if (raw.isEmpty()) return "0"
        val whole = raw.substringBefore('.')
        val grouped = whole.reversed().chunked(3).joinToString(",").reversed()
        return if (raw.contains('.')) grouped + "." + raw.substringAfter('.') else grouped
    }

    private fun nearHint(raw: String, rate: String): String = try {
        val naira = BigDecimal(if (raw.isEmpty() || raw == ".") "0" else raw.trimEnd('.'))
        val near = naira.divide(BigDecimal(rate), 4, java.math.RoundingMode.HALF_UP).stripTrailingZeros().toPlainString()
        "≈ $near NEAR at the demo rate"
    } catch (e: Exception) {
        "Enter an amount"
    }
}
