package com.wayfarer.tap

import java.io.File
import java.math.BigDecimal
import java.math.BigInteger
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertTrue
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private fun vectors(name: String): JsonObject =
    Json.parseToJsonElement(File(System.getProperty("wayfarer.vectors"), name).readText()).jsonObject

private fun JsonObject.text(key: String) = getValue(key).jsonPrimitive.content

private fun requestFrom(json: JsonObject) = PaymentRequest(
    merchantAccount = json.text("merchant_account"),
    merchantName = json.text("merchant_name"),
    fiatCurrency = json.text("fiat_currency"),
    fiatMinor = json.text("fiat_minor").toLong(),
    fiatExponent = json.text("fiat_exponent").toInt(),
    tokenId = json.text("token_id"),
    assetAmount = BigInteger(json.text("asset_amount")),
    quoteId = json.text("quote_id"),
    rate = json.text("rate"),
    quoteExpiresMs = json.text("quote_expires_ms").toLong(),
    settleByMs = json.text("settle_by_ms").toLong(),
    nonce = json.text("nonce").hexToBytes(),
    createdMs = json.text("created_ms").toLong(),
)

/** Cross-language checks: the Kotlin code must reproduce what the Python package wrote into tests/vectors/tap.json. */
class TapVectorTest {
    private val tap = vectors("tap.json")
    private val request = requestFrom(tap.getValue("request").jsonObject)
    private val seed = tap.text("buyer_seed").hexToBytes()
    private val vault = tap.text("vault")
    private val now = tap.text("now_ms").toLong()

    @Test
    fun `request bytes, hash, ids and display match Python`() {
        assertEquals(tap.text("request_message_hex"), request.message().toHex())
        assertEquals(tap.text("request_hash_hex"), request.requestHash().toHex())
        assertEquals(tap.text("intent_id"), request.intentId())
        assertEquals(tap.text("receipt_hash"), request.receiptHash())
        assertEquals(tap.text("fiat_display"), request.fiatDisplay())
        assertEquals(request, PaymentRequest.decode(request.message()))
    }

    @Test
    fun `cheque message matches Python`() {
        assertEquals(tap.text("cheque_message_hex"), chequeForRequest(request, vault).message().toHex())
    }

    @Test
    fun `the seed gives the same public key and the Python signature verifies`() {
        val publicKey = MlDsa.publicKeyFromSeed(seed)
        assertEquals(tap.text("buyer_public_key_sha256"), sha256(publicKey).toHex())
        val message = chequeForRequest(request, vault).message()
        assertTrue(MlDsa.verify(publicKey, tap.text("signature_hex").hexToBytes(), message))
    }

    @Test
    fun `deterministic signing reproduces the Python signature byte for byte`() {
        val message = chequeForRequest(request, vault).message()
        assertEquals(tap.text("signature_hex"), MlDsa.sign(seed, message, deterministic = true).toHex())
    }

    @Test
    fun `the buyer wallet produces exactly the envelope Python made`() {
        val wallet = BuyerWallet(vault, SeedSigner(seed, deterministic = true), { true }, mapOf("near" to BigInteger.TEN.pow(24)), { now })
        assertEquals(tap.text("envelope_hex"), wallet.pay(request).encode().toHex())
    }

    @Test
    fun `QR frames match Python and reassemble in any order with duplicates`() {
        val envelope = tap.text("envelope_hex").hexToBytes()
        val expected = tap.getValue("envelope_frames").jsonArray.map { it.jsonPrimitive.content }
        assertEquals(expected, encodeFrames(envelope, "a1b2c3d4".hexToBytes()))
        assertEquals(
            tap.getValue("request_frames").jsonArray.map { it.jsonPrimitive.content },
            encodeFrames(request.message(), "a1b2c3d4".hexToBytes()),
        )
        val assembler = FrameAssembler()
        (expected.reversed() + expected.take(2)).forEach { assembler.add(it) }
        assertContentEquals(envelope, assembler.payload())
    }

    @Test
    fun `damaged, foreign and missing frames are refused`() {
        val frames = tap.getValue("envelope_frames").jsonArray.map { it.jsonPrimitive.content }
        val assembler = FrameAssembler()
        assertFailsWith<TapException> { assembler.add(frames[0].replace("V0ZD", "V0ZE")) }
        assembler.add(frames[0])
        assertFailsWith<TapException> { assembler.add("WF1.00000000.1.6.AAAA." + "00000000") }
        assertFailsWith<TapException> { assembler.payload() }
        assertEquals(listOf(1, 2, 3, 4, 5), assembler.progress().missing)
    }

    @Test
    fun `merchant accepts the Python cheque offline and refuses tampering`() {
        val terminal = MerchantTerminal(
            request.merchantAccount, request.merchantName, { BigDecimal("5000") }, { now },
            newNonce = { request.nonce }, newQuoteId = { request.quoteId },
        )
        val own = terminal.createRequest(request.fiatMinor)
        assertEquals(request, own)
        val raw = tap.text("envelope_hex").hexToBytes()
        val tampered = raw.copyOf().also { it[raw.size - 5] = (it[raw.size - 5].toInt() xor 1).toByte() }
        assertFailsWith<TapException> { terminal.accept(tampered) }
        val accepted = terminal.accept(raw)
        assertEquals(tap.text("buyer_public_key_sha256"), sha256(accepted.publicKey).toHex())
        assertFailsWith<TapException> { terminal.accept(raw) } // already accepted
    }

    @Test
    fun `rounding is up and matches the Python rate desk`() {
        assertEquals(BigInteger("300000000000000000000000"), assetAmountFor(150000, 2, BigDecimal("5000"), "near"))
        assertEquals(BigInteger("333333333333333333333334"), assetAmountFor(100000, 2, BigDecimal("3000"), "near"))
    }

    @Test
    fun `the buyer wallet refuses what it should`() {
        val limit = mapOf("near" to BigInteger.TEN.pow(23)) // 0.1 NEAR, below the 0.3 NEAR request
        val strict = BuyerWallet(vault, SeedSigner(seed), { true }, limit, { now })
        assertEquals("That is above your tap limit of 0.100000 NEAR.", assertFailsWith<TapException> { strict.pay(request) }.message)
        val no = BuyerWallet(vault, SeedSigner(seed), { false }, mapOf("near" to BigInteger.TEN.pow(24)), { now })
        assertEquals("Payment cancelled.", assertFailsWith<TapException> { no.pay(request) }.message)
        val late = BuyerWallet(vault, SeedSigner(seed), { true }, mapOf("near" to BigInteger.TEN.pow(24)), { request.quoteExpiresMs + 1 })
        assertFailsWith<TapException> { late.review(request) }
        val yes = BuyerWallet(vault, SeedSigner(seed), { true }, mapOf("near" to BigInteger.TEN.pow(24)), { now })
        yes.pay(request)
        assertFailsWith<TapException> { yes.pay(request) }
    }

    @Test
    fun `two phones over a loopback link complete a payment`() {
        val (merchantEnd, buyerEnd) = LoopbackLink.pair()
        val terminal = MerchantTerminal(
            request.merchantAccount, request.merchantName, { BigDecimal("5000") }, { now },
            newNonce = { request.nonce }, newQuoteId = { request.quoteId },
        )
        merchantEnd.send(terminal.createRequest(request.fiatMinor).message())
        val wallet = BuyerWallet(vault, SeedSigner(seed), { true }, mapOf("near" to BigInteger.TEN.pow(24)), { now })
        buyerEnd.send(wallet.pay(PaymentRequest.decode(assertNotNull(buyerEnd.receive()))).encode())
        val accepted = terminal.accept(assertNotNull(merchantEnd.receive()))
        assertEquals(BigInteger("300000000000000000000000"), accepted.cheque.amount)
    }
}

class MlDsaVectorTest {
    @Test
    fun `near-crypto vectors verify and altered input is rejected`() {
        val cases = vectors("mldsa65.json").getValue("cases") as JsonArray
        for (case in cases.map { it.jsonObject }) {
            val publicKey = case.text("public_key").hexToBytes()
            val message = case.text("message").hexToBytes()
            val seed = case.text("seed").hexToBytes()
            assertContentEquals(publicKey, MlDsa.publicKeyFromSeed(seed))
            assertTrue(MlDsa.verify(publicKey, case.text("signature_reference").hexToBytes(), message))
            assertEquals(case.text("signature_deterministic"), MlDsa.sign(seed, message, deterministic = true).toHex())
            assertTrue(MlDsa.verify(publicKey, MlDsa.sign(seed, message), message)) // hedged signing round-trips
            assertFalse(MlDsa.verify(publicKey, case.text("signature_reference").hexToBytes(), message.copyOf().also { it[0] = (it[0].toInt() xor 1).toByte() }))
        }
    }
}

class CodecTest {
    @Test
    fun `strict decoding refuses truncated, trailing and oversize input`() {
        val message = Writer().string("hi").u64(7).toByteArray()
        val reader = Reader(message)
        assertEquals("hi", reader.string())
        assertEquals(7L, reader.u64())
        reader.finish()
        assertFailsWith<TapException> { Reader(message.copyOf(5)).let { it.string(); it.u64() } }
        assertFailsWith<TapException> { Reader(message + byteArrayOf(0)).let { it.string(); it.u64(); it.finish() } }
        assertFailsWith<TapException> { Writer().string("x".repeat(257)) }
        assertFailsWith<TapException> { Reader(Writer().u32(300).toByteArray()).string() }
        assertFailsWith<TapException> { PaymentRequest.decode(ByteArray(10)) }
    }
}
