package com.wayfarer.tap

import java.math.BigDecimal
import java.math.BigInteger
import java.math.MathContext
import java.math.RoundingMode
import java.security.MessageDigest

private val ACCOUNT = Regex("(?:[a-z0-9]+(?:[-_][a-z0-9]+)*\\.)+(?:near|testnet)|[0-9a-f]{64}")
private val CURRENCY = Regex("[A-Z]{3}")
private val U64_MAX = BigInteger.ONE.shiftLeft(63).subtract(BigInteger.ONE) // Kotlin Long limit; see Reader.u64
private val U128_MAX = BigInteger.ONE.shiftLeft(128).subtract(BigInteger.ONE)
const val NONCE_BYTES = 16
val TOKEN_DECIMALS = mapOf("near" to 24)

fun isValidNearAccount(value: String) = value.length <= 64 && ACCOUNT.matches(value)

fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

fun String.hexToBytes(): ByteArray {
    if (length % 2 != 0) throw TapException("Invalid hex.")
    return ByteArray(length / 2) { i -> substring(2 * i, 2 * i + 2).toInt(16).toByte() }
}

/** What the buyer's phone is asked to pay. `assetAmount` is in the token's smallest unit. */
data class PaymentRequest(
    val merchantAccount: String,
    val merchantName: String,
    val fiatCurrency: String,
    val fiatMinor: Long,
    val fiatExponent: Int,
    val tokenId: String,
    val assetAmount: BigInteger,
    val quoteId: String,
    val rate: String,
    val quoteExpiresMs: Long,
    val settleByMs: Long,
    val nonce: ByteArray,
    val createdMs: Long,
) {
    init {
        if (!isValidNearAccount(merchantAccount)) throw TapException("The merchant account is not a valid NEAR account.")
        if (merchantName.length !in 1..64 || merchantName.any { it.isISOControl() }) {
            throw TapException("The merchant name must be 1-64 printable characters.")
        }
        if (!CURRENCY.matches(fiatCurrency)) throw TapException("The currency must be a 3-letter ISO code.")
        if (fiatMinor <= 0 || fiatExponent !in 0..6) throw TapException("The local amount is out of range.")
        if (assetAmount.signum() <= 0 || assetAmount > U128_MAX) throw TapException("The asset amount is out of range.")
        if (tokenId.isEmpty() || tokenId.length > 64) throw TapException("The token id is invalid.")
        if (nonce.size != NONCE_BYTES) throw TapException("The nonce must be $NONCE_BYTES bytes.")
        if (!(0 < createdMs && createdMs < quoteExpiresMs && quoteExpiresMs <= settleByMs)) {
            throw TapException("The request times are inconsistent.")
        }
        val price = rate.toBigDecimalOrNull() ?: throw TapException("The rate is not a number.")
        if (price.signum() <= 0) throw TapException("The rate must be positive.")
    }

    fun message(): ByteArray = Writer()
        .raw(REQUEST_DOMAIN)
        .string(merchantAccount).string(merchantName).string(fiatCurrency)
        .u64(fiatMinor).u8(fiatExponent)
        .string(tokenId).u128(assetAmount).string(quoteId).string(rate)
        .u64(quoteExpiresMs).u64(settleByMs).raw(nonce).u64(createdMs)
        .toByteArray()

    fun requestHash(): ByteArray = sha256(message())

    fun intentId() = "tap-" + nonce.toHex()

    fun receiptHash() = "wft:" + requestHash().toHex()

    fun fiatDisplay(): String {
        val value = BigDecimal(fiatMinor).movePointLeft(fiatExponent)
        return "$fiatCurrency " + java.text.DecimalFormat("#,##0" + if (fiatExponent > 0) "." + "0".repeat(fiatExponent) else "",
            java.text.DecimalFormatSymbols(java.util.Locale.ROOT)).format(value)
    }

    override fun equals(other: Any?) = other is PaymentRequest && message().contentEquals(other.message())

    override fun hashCode() = message().contentHashCode()

    companion object {
        val REQUEST_DOMAIN = "wayfarer-request-v1".toByteArray()

        fun decode(data: ByteArray): PaymentRequest {
            val reader = Reader(data)
            if (!reader.raw(REQUEST_DOMAIN.size).contentEquals(REQUEST_DOMAIN)) {
                throw TapException("This is not a Wayfarer payment request.")
            }
            val request = PaymentRequest(
                merchantAccount = reader.string(),
                merchantName = reader.string(),
                fiatCurrency = reader.string(),
                fiatMinor = reader.u64(),
                fiatExponent = reader.u8(),
                tokenId = reader.string(),
                assetAmount = reader.u128(),
                quoteId = reader.string(),
                rate = reader.string(),
                quoteExpiresMs = reader.u64(),
                settleByMs = reader.u64(),
                nonce = reader.raw(NONCE_BYTES),
                createdMs = reader.u64(),
            )
            reader.finish()
            return request
        }
    }
}

/** What the buyer's phone signs and the vault contract checks. The layout is the same as `Cheque.message()` in Python. */
data class Cheque(
    val vault: String,
    val tokenId: String,
    val to: String,
    val amount: BigInteger,
    val intentId: String,
    val expiresAtMs: Long,
    val receiptHash: String,
) {
    init {
        if (amount.signum() <= 0 || amount > U128_MAX) throw TapException("Cheque amount must be positive and fit in 128 bits.")
        if (expiresAtMs <= 0) throw TapException("Cheque expiry must be a positive unix-millisecond time.")
        if (intentId.length !in 1..64) throw TapException("Cheque intent id must be 1-64 characters.")
        if (receiptHash.length > 128) throw TapException("Cheque receipt hash is too long.")
    }

    fun message(): ByteArray = Writer()
        .raw(CHEQUE_DOMAIN)
        .string(vault).string(tokenId).string(to).u128(amount).string(intentId).u64(expiresAtMs).string(receiptHash)
        .toByteArray()

    companion object {
        val CHEQUE_DOMAIN = "wayfarer-cheque-v1".toByteArray()
    }
}

/** The exact cheque a buyer signs for `request`: pays the merchant, expires at `settleByMs`. */
fun chequeForRequest(request: PaymentRequest, vault: String) = Cheque(
    vault = vault,
    tokenId = request.tokenId,
    to = request.merchantAccount,
    amount = request.assetAmount,
    intentId = request.intentId(),
    expiresAtMs = request.settleByMs,
    receiptHash = request.receiptHash(),
)

/** Token amount in the smallest unit for a local amount, rounded UP so the merchant never receives less. */
fun assetAmountFor(fiatMinor: Long, fiatExponent: Int, fiatPerToken: BigDecimal, tokenId: String): BigInteger {
    val decimals = TOKEN_DECIMALS[tokenId] ?: throw TapException("$tokenId is not supported for tap payments.")
    if (fiatPerToken.signum() <= 0) throw TapException("The rate must be positive.")
    val fiat = BigDecimal(fiatMinor).movePointLeft(fiatExponent)
    val tokens = fiat.divide(fiatPerToken, MathContext(80, RoundingMode.HALF_EVEN))
    return tokens.movePointRight(decimals).setScale(0, RoundingMode.CEILING).toBigIntegerExact()
}

/** The buyer's reply: a signed cheque. The cheque itself is not sent; the merchant rebuilds it from its own request. */
class ChequeEnvelope(
    val requestHash: ByteArray,
    val vault: String,
    val keyId: ByteArray,
    val signature: ByteArray,
    val publicKey: ByteArray?,
) {
    init {
        if (requestHash.size != 32 || keyId.size != 32) throw TapException("The envelope hashes must be 32 bytes.")
        if (signature.size != MlDsa.SIGNATURE_SIZE) throw TapException("The signature has the wrong size.")
        if (publicKey != null && publicKey.size != MlDsa.PUBLIC_KEY_SIZE) throw TapException("The public key has the wrong size.")
        if (!isValidNearAccount(vault)) throw TapException("The vault is not a valid NEAR account.")
    }

    fun encode(): ByteArray {
        val writer = Writer().raw(MAGIC).raw(requestHash).string(vault).raw(keyId).u8(if (publicKey == null) 0 else 1)
        if (publicKey != null) writer.raw(publicKey)
        return writer.raw(signature).toByteArray()
    }

    companion object {
        val MAGIC = "WFC1".toByteArray()

        fun decode(data: ByteArray): ChequeEnvelope {
            val reader = Reader(data)
            if (!reader.raw(MAGIC.size).contentEquals(MAGIC)) throw TapException("This is not a Wayfarer cheque.")
            val requestHash = reader.raw(32)
            val vault = reader.string()
            val keyId = reader.raw(32)
            val flag = reader.u8()
            if (flag != 0 && flag != 1) throw TapException("The envelope flags are invalid.")
            val publicKey = if (flag == 1) reader.raw(MlDsa.PUBLIC_KEY_SIZE) else null
            val signature = reader.raw(MlDsa.SIGNATURE_SIZE)
            reader.finish()
            return ChequeEnvelope(requestHash, vault, keyId, signature, publicKey)
        }
    }
}
