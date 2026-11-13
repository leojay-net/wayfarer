package com.wayfarer.tap

import java.math.BigDecimal
import java.math.BigInteger
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class ApduTest {
    private val now = 1_800_000_000_000L
    private val vault = "buyer-vault.testnet"

    private fun terminal() = MerchantTerminal("ada-shop.testnet", "Ada's Shop", { BigDecimal("5000") }, { now })

    /** Wire a server and client together, checking that no command or reply exceeds the short-APDU limits. */
    private fun wire(server: ApduServer, log: MutableList<ByteArray> = mutableListOf()): ApduClient = ApduClient { command ->
        assertTrue(command.size <= 261, "command too long: ${command.size}")
        log += command
        server.process(command).also { assertTrue(it.size <= 258, "reply too long: ${it.size}") }
    }

    @Test
    fun `a full tap moves the request one way and the 5 KB cheque back`() {
        val merchant = terminal()
        val request = merchant.createRequest(150_000)
        var accepted: AcceptedCheque? = null
        val server = ApduServer { raw ->
            accepted = merchant.accept(raw)
            CommitResult(true, "Accepted, pending settlement")
        }
        server.outgoing = request.message()
        val log = mutableListOf<ByteArray>()
        val client = wire(server, log)

        val received = PaymentRequest.decode(client.fetchRequest())
        assertEquals(request, received)

        val wallet = BuyerWallet(vault, SeedSigner(ByteArray(32) { it.toByte() }), { true }, mapOf("near" to BigInteger.TEN.pow(24)), { now })
        val envelope = wallet.pay(received).encode()
        assertTrue(envelope.size > 5000)
        val result = client.sendCheque(envelope)
        assertTrue(result.accepted)
        assertEquals("Accepted, pending settlement", result.text)
        assertEquals(request.assetAmount, accepted!!.cheque.amount)
        // The cheque needed ceil(size / 240) writes plus select, size, reads and one commit.
        assertTrue(log.count { (it[1].toInt() and 0xff) == Apdu.INS_WRITE } == (envelope.size + 239) / 240)
    }

    @Test
    fun `a refused cheque comes back as a refusal with the reason`() {
        val merchant = terminal()
        val request = merchant.createRequest(150_000)
        val server = ApduServer { raw -> CommitResult(true, "ok").also { merchant.accept(raw) } }
        server.outgoing = request.message()
        val client = wire(server)
        client.fetchRequest()
        val garbage = ByteArray(300) { 7 }
        val result = client.sendCheque(garbage)
        assertFalse(result.accepted)
        assertEquals("This is not a Wayfarer cheque.", result.text)
    }

    @Test
    fun `the card ignores everything until its own AID is selected`() {
        val server = ApduServer { CommitResult(true, "ok") }
        server.outgoing = byteArrayOf(1, 2, 3)
        val read = byteArrayOf(Apdu.CLA.toByte(), Apdu.INS_READ.toByte(), 0, 0, 3)
        assertEquals(Apdu.SW_NOT_FOUND, Apdu.statusOf(server.process(read)))
        val wrongAid = byteArrayOf(0x00, 0xA4.toByte(), 0x04, 0x00, 2, 1, 2)
        assertEquals(Apdu.SW_NOT_FOUND, Apdu.statusOf(server.process(wrongAid)))
        assertEquals(Apdu.SW_OK, Apdu.statusOf(server.process(Apdu.selectCommand())))
        assertContentEquals(byteArrayOf(1, 2, 3), Apdu.dataOf(server.process(read)))
        assertEquals(Apdu.SW_BAD_INS, Apdu.statusOf(server.process(byteArrayOf(Apdu.CLA.toByte(), 0x77, 0, 0))))
        assertEquals(Apdu.SW_WRONG_LENGTH, Apdu.statusOf(server.process(byteArrayOf(1, 2))))
    }

    @Test
    fun `out of order writes and a wrong total are refused and nothing is delivered`() {
        var delivered = 0
        val server = ApduServer { delivered++; CommitResult(true, "ok") }
        server.process(Apdu.selectCommand())
        val skip = byteArrayOf(Apdu.CLA.toByte(), Apdu.INS_WRITE.toByte(), 0, 10, 2, 9, 9)
        assertEquals(Apdu.SW_BAD_OFFSET, Apdu.statusOf(server.process(skip)))
        val first = byteArrayOf(Apdu.CLA.toByte(), Apdu.INS_WRITE.toByte(), 0, 0, 2, 9, 9)
        assertEquals(Apdu.SW_OK, Apdu.statusOf(server.process(first)))
        val commit = byteArrayOf(Apdu.CLA.toByte(), Apdu.INS_COMMIT.toByte(), 0, 0, 4) + Writer().u32(5).toByteArray()
        assertEquals(Apdu.SW_BAD_OFFSET, Apdu.statusOf(server.process(commit)))
        assertEquals(0, delivered)
    }

    @Test
    fun `a missing request, errors from the card and unknown phones are reported`() {
        val bare = ApduServer { CommitResult(true, "ok") }
        assertFailsWith<TapException> { wire(bare).fetchRequest() }.also { assertTrue(it.message!!.isNotEmpty()) }
        val deaf = ApduClient { byteArrayOf(0x6A, 0x82.toByte()) }
        assertFailsWith<TapException> { deaf.fetchRequest() }
        val shortReply = ApduClient { byteArrayOf(0x90.toByte()) }
        assertFailsWith<TapException> { shortReply.fetchRequest() }
    }
}
