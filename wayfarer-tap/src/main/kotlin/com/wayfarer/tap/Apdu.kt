package com.wayfarer.tap

/**
 * The NFC data protocol between two phones, as plain ISO 7816-4 short APDUs so the Android glue stays thin.
 *
 * The MERCHANT phone is the "card" (host card emulation) and holds the outgoing payment request; the BUYER phone is the
 * reader. The reader SELECTs our AID, reads the request in chunks, then writes the signed cheque in chunks and commits.
 * Every message is chunked to at most [MAX_CHUNK] bytes because short APDUs carry 255; a 5.4 KB cheque is ~23 writes.
 *
 * STATUS: the state machines below are unit-tested here. The Android glue (HostApduService, IsoDep) and real-phone
 * throughput are NOT tested on hardware. Measure before trusting the timing.
 */
object Apdu {
    /** Proprietary application id: 0xF0 followed by the ASCII letters "WAYFAR". */
    val AID = byteArrayOf(0xF0.toByte(), 0x57, 0x41, 0x59, 0x46, 0x41, 0x52)
    const val MAX_CHUNK = 240
    const val CLA = 0x80
    const val INS_SIZE = 0xB2
    const val INS_READ = 0xB0
    const val INS_WRITE = 0xD0
    const val INS_COMMIT = 0xD1
    const val SW_OK = 0x9000
    const val SW_REFUSED = 0x6985
    const val SW_BAD_OFFSET = 0x6A86
    const val SW_NOT_FOUND = 0x6A82
    const val SW_WRONG_LENGTH = 0x6700
    const val SW_BAD_INS = 0x6D00

    fun selectCommand(): ByteArray = byteArrayOf(0x00, 0xA4.toByte(), 0x04, 0x00, AID.size.toByte()) + AID

    fun response(data: ByteArray, status: Int): ByteArray =
        data + byteArrayOf((status shr 8).toByte(), status.toByte())

    fun statusOf(response: ByteArray): Int {
        if (response.size < 2) throw TapException("The other phone sent a short reply.")
        return ((response[response.size - 2].toInt() and 0xff) shl 8) or (response[response.size - 1].toInt() and 0xff)
    }

    fun dataOf(response: ByteArray): ByteArray = response.copyOf(response.size - 2)
}

/** The merchant's reply to a committed cheque: accepted means PENDING SETTLEMENT, never paid. */
data class CommitResult(val accepted: Boolean, val text: String)

/** The card side. Hand `process` every command the NFC stack delivers; it returns the reply. One transfer at a time. */
class ApduServer(private val onCheque: (ByteArray) -> CommitResult) {
    @Volatile
    var outgoing: ByteArray? = null

    private var inbox = ByteArray(0)
    private var selected = false

    @Synchronized
    fun reset() {
        inbox = ByteArray(0)
        selected = false
    }

    @Synchronized
    fun process(command: ByteArray): ByteArray {
        if (command.size < 4) return Apdu.response(ByteArray(0), Apdu.SW_WRONG_LENGTH)
        val cla = command[0].toInt() and 0xff
        val ins = command[1].toInt() and 0xff
        if (cla == 0x00 && ins == 0xA4) return select(command)
        if (!selected || cla != Apdu.CLA) return Apdu.response(ByteArray(0), Apdu.SW_NOT_FOUND)
        val offset = ((command[2].toInt() and 0xff) shl 8) or (command[3].toInt() and 0xff)
        return when (ins) {
            Apdu.INS_SIZE -> {
                val out = outgoing ?: return Apdu.response(ByteArray(0), Apdu.SW_NOT_FOUND)
                Apdu.response(Writer().u32(out.size.toLong()).toByteArray(), Apdu.SW_OK)
            }
            Apdu.INS_READ -> read(command, offset)
            Apdu.INS_WRITE -> write(command, offset)
            Apdu.INS_COMMIT -> commit(command)
            else -> Apdu.response(ByteArray(0), Apdu.SW_BAD_INS)
        }
    }

    private fun select(command: ByteArray): ByteArray {
        val length = if (command.size > 4) command[4].toInt() and 0xff else 0
        val aid = if (command.size >= 5 + length) command.copyOfRange(5, 5 + length) else ByteArray(0)
        if (!aid.contentEquals(Apdu.AID)) return Apdu.response(ByteArray(0), Apdu.SW_NOT_FOUND)
        selected = true
        inbox = ByteArray(0)
        return Apdu.response(ByteArray(0), Apdu.SW_OK)
    }

    private fun read(command: ByteArray, offset: Int): ByteArray {
        val out = outgoing ?: return Apdu.response(ByteArray(0), Apdu.SW_NOT_FOUND)
        val wanted = if (command.size > 4) (command[4].toInt() and 0xff).let { if (it == 0) 256 else it } else Apdu.MAX_CHUNK
        if (offset > out.size) return Apdu.response(ByteArray(0), Apdu.SW_BAD_OFFSET)
        val end = minOf(out.size, offset + minOf(wanted, Apdu.MAX_CHUNK))
        return Apdu.response(out.copyOfRange(offset, end), Apdu.SW_OK)
    }

    private fun write(command: ByteArray, offset: Int): ByteArray {
        if (command.size < 5) return Apdu.response(ByteArray(0), Apdu.SW_WRONG_LENGTH)
        val length = command[4].toInt() and 0xff
        if (length > Apdu.MAX_CHUNK || command.size < 5 + length) return Apdu.response(ByteArray(0), Apdu.SW_WRONG_LENGTH)
        if (offset != inbox.size) return Apdu.response(ByteArray(0), Apdu.SW_BAD_OFFSET)
        if (inbox.size + length > MAX_MESSAGE) return Apdu.response(ByteArray(0), Apdu.SW_WRONG_LENGTH)
        inbox += command.copyOfRange(5, 5 + length)
        return Apdu.response(ByteArray(0), Apdu.SW_OK)
    }

    private fun commit(command: ByteArray): ByteArray {
        if (command.size < 9 || command[4].toInt() != 4) return Apdu.response(ByteArray(0), Apdu.SW_WRONG_LENGTH)
        val expected = Reader(command.copyOfRange(5, 9)).u32()
        if (expected != inbox.size.toLong()) return Apdu.response(ByteArray(0), Apdu.SW_BAD_OFFSET)
        val message = inbox
        inbox = ByteArray(0)
        val result = try {
            onCheque(message)
        } catch (e: TapException) {
            CommitResult(false, e.message ?: "Refused.")
        }
        val text = result.text.toByteArray(Charsets.UTF_8).let { if (it.size > Apdu.MAX_CHUNK) it.copyOf(Apdu.MAX_CHUNK) else it }
        return Apdu.response(text, if (result.accepted) Apdu.SW_OK else Apdu.SW_REFUSED)
    }

    companion object {
        const val MAX_MESSAGE = 8 * 1024
    }
}

/** The reader side. `transceive` sends one command APDU and returns the full response (data plus status word). */
class ApduClient(private val transceive: (ByteArray) -> ByteArray) {
    private fun send(command: ByteArray): ByteArray {
        val reply = transceive(command)
        val status = Apdu.statusOf(reply)
        if (status != Apdu.SW_OK && status != Apdu.SW_REFUSED) throw TapException("The other phone answered with error %04X.".format(status))
        return reply
    }

    fun select() {
        val reply = transceive(Apdu.selectCommand())
        if (Apdu.statusOf(reply) != Apdu.SW_OK) throw TapException("That phone is not a Wayfarer merchant terminal.")
    }

    private fun header(ins: Int, offset: Int) = byteArrayOf(Apdu.CLA.toByte(), ins.toByte(), (offset shr 8).toByte(), offset.toByte())

    /** SELECT the merchant app and read the whole payment request. */
    fun fetchRequest(): ByteArray {
        select()
        val sizeReply = send(header(Apdu.INS_SIZE, 0) + byteArrayOf(4))
        val size = Reader(Apdu.dataOf(sizeReply)).u32().toInt()
        if (size !in 1..ApduServer.MAX_MESSAGE) throw TapException("The payment request has an invalid size.")
        var out = ByteArray(0)
        while (out.size < size) {
            val chunk = Apdu.dataOf(send(header(Apdu.INS_READ, out.size) + byteArrayOf(minOf(Apdu.MAX_CHUNK, size - out.size).toByte())))
            if (chunk.isEmpty()) throw TapException("The payment request ended early.")
            out += chunk
        }
        return out.copyOf(size)
    }

    /** Write the cheque envelope back and commit it. The result says whether the merchant accepted it (pending settlement). */
    fun sendCheque(envelope: ByteArray): CommitResult {
        if (envelope.size > ApduServer.MAX_MESSAGE) throw TapException("The cheque is too large.")
        var sent = 0
        while (sent < envelope.size) {
            val chunk = envelope.copyOfRange(sent, minOf(envelope.size, sent + Apdu.MAX_CHUNK))
            send(header(Apdu.INS_WRITE, sent) + byteArrayOf(chunk.size.toByte()) + chunk)
            sent += chunk.size
        }
        val reply = send(header(Apdu.INS_COMMIT, 0) + byteArrayOf(4) + Writer().u32(envelope.size.toLong()).toByteArray())
        return CommitResult(Apdu.statusOf(reply) == Apdu.SW_OK, String(Apdu.dataOf(reply), Charsets.UTF_8))
    }
}
