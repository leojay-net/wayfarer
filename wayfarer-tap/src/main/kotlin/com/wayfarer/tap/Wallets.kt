package com.wayfarer.tap

import java.math.BigDecimal
import java.math.BigInteger

/** What the human sees before confirming, in their own terms first. */
data class ApprovalPrompt(
    val merchantName: String,
    val merchantAccount: String,
    val fiatText: String,
    val assetText: String,
    val settleByMs: Long,
) {
    fun lines() = listOf(
        "Pay $fiatText to $merchantName",
        "= $assetText from your vault",
        "Merchant account: $merchantAccount",
    )
}

/** The human approval step (biometric or device PIN on a phone). Must return true only for an explicit yes. */
fun interface Confirmer {
    fun confirm(prompt: ApprovalPrompt): Boolean
}

/** Signs cheque bytes. The Android app keeps the seed encrypted under a Keystore key; tests use [SeedSigner]. */
interface ChequeSigner {
    val publicKey: ByteArray

    fun sign(message: ByteArray): ByteArray
}

class SeedSigner(private val seed: ByteArray, private val deterministic: Boolean = false) : ChequeSigner {
    override val publicKey: ByteArray = MlDsa.publicKeyFromSeed(seed)

    override fun sign(message: ByteArray) = MlDsa.sign(seed, message, deterministic)
}

fun assetText(tokenId: String, amount: BigInteger): String {
    val decimals = TOKEN_DECIMALS[tokenId] ?: return "$amount $tokenId (smallest units)"
    return BigDecimal(amount).movePointLeft(decimals).setScale(6, java.math.RoundingMode.HALF_EVEN).toPlainString() +
        " " + tokenId.uppercase()
}

/** The buyer's phone: review a request, ask the human, sign a cheque. Nothing is signed unless the human says yes. */
class BuyerWallet(
    private val vault: String,
    private val signer: ChequeSigner,
    private val confirmer: Confirmer,
    private val perTapLimits: Map<String, BigInteger>,
    private val clockMs: () -> Long,
    private val includePublicKey: Boolean = true,
) {
    private val signed = mutableSetOf<String>()

    fun review(request: PaymentRequest): ApprovalPrompt {
        if (clockMs() > request.quoteExpiresMs) throw TapException("This price has expired. Ask the merchant for a new one.")
        val limit = perTapLimits[request.tokenId]
            ?: throw TapException("Paying with ${request.tokenId} is not enabled on this phone.")
        if (request.assetAmount > limit) {
            throw TapException("That is above your tap limit of ${assetText(request.tokenId, limit)}.")
        }
        if (request.nonce.toHex() in signed) throw TapException("You already approved this request.")
        return ApprovalPrompt(
            request.merchantName, request.merchantAccount, request.fiatDisplay(),
            assetText(request.tokenId, request.assetAmount), request.settleByMs,
        )
    }

    fun pay(request: PaymentRequest): ChequeEnvelope {
        val prompt = review(request)
        if (!confirmer.confirm(prompt)) throw TapException("Payment cancelled.")
        if (clockMs() > request.quoteExpiresMs) throw TapException("This price expired while you were deciding.")
        val signature = signer.sign(chequeForRequest(request, vault).message())
        signed += request.nonce.toHex()
        return ChequeEnvelope(request.requestHash(), vault, sha256(signer.publicKey), signature, if (includePublicKey) signer.publicKey else null)
    }
}

data class AcceptedCheque(
    val request: PaymentRequest,
    val cheque: Cheque,
    val keyId: String,
    val signature: ByteArray,
    val publicKey: ByteArray,
)

/** The merchant's phone: price a sale and accept a signed cheque offline. Accepted means PENDING SETTLEMENT, not paid. */
class MerchantTerminal(
    private val account: String,
    private val name: String,
    private val fiatPerToken: () -> BigDecimal,
    private val clockMs: () -> Long,
    private val settleWindowMs: Long = 10 * 60_000,
    private val lockMs: Long = 60_000,
    private val newNonce: () -> ByteArray = { ByteArray(NONCE_BYTES).also(java.security.SecureRandom()::nextBytes) },
    private val newQuoteId: () -> String = { java.util.UUID.randomUUID().toString().take(16) },
) {
    private val open = mutableMapOf<String, PaymentRequest>()
    private val accepted = mutableSetOf<String>()

    fun createRequest(fiatMinor: Long, fiatCurrency: String = "USD", fiatExponent: Int = 2, tokenId: String = "near"): PaymentRequest {
        val price = fiatPerToken()
        val now = clockMs()
        val request = PaymentRequest(
            merchantAccount = account,
            merchantName = name,
            fiatCurrency = fiatCurrency,
            fiatMinor = fiatMinor,
            fiatExponent = fiatExponent,
            tokenId = tokenId,
            assetAmount = assetAmountFor(fiatMinor, fiatExponent, price, tokenId),
            quoteId = newQuoteId(),
            rate = price.toPlainString(),
            quoteExpiresMs = now + lockMs,
            settleByMs = now + lockMs + settleWindowMs,
            nonce = newNonce(),
            createdMs = now,
        )
        open[request.requestHash().toHex()] = request
        return request
    }

    fun accept(raw: ByteArray): AcceptedCheque {
        val envelope = ChequeEnvelope.decode(raw)
        val hash = envelope.requestHash.toHex()
        val request = open[hash] ?: throw TapException("This cheque is not for any request of this terminal.")
        if (hash in accepted) throw TapException("A cheque for this request was already accepted.")
        if (clockMs() > request.settleByMs) throw TapException("This request is too old to settle.")
        val publicKey = envelope.publicKey
            ?: throw TapException("The cheque does not carry the buyer's public key; it cannot be checked offline.")
        if (!sha256(publicKey).contentEquals(envelope.keyId)) throw TapException("The key id does not match the public key.")
        val cheque = chequeForRequest(request, envelope.vault)
        if (!MlDsa.verify(publicKey, envelope.signature, cheque.message())) {
            throw TapException("The cheque signature is not valid for this request.")
        }
        accepted += hash
        return AcceptedCheque(request, cheque, envelope.keyId.toHex(), envelope.signature, publicKey)
    }
}
