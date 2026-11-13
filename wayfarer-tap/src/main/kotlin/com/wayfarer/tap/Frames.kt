package com.wayfarer.tap

import java.util.Base64
import java.util.zip.CRC32

const val FRAME_PREFIX = "WF1"
const val DEFAULT_CHUNK_BYTES = 900

/** Moves bytes between two phones. NFC, Nearby and QR implement this; tests use [LoopbackLink]. */
interface PaymentLink {
    fun send(data: ByteArray)

    /** The next message, or null if none arrived within `timeoutMs`. */
    fun receive(timeoutMs: Long = 5000): ByteArray?
}

class LoopbackLink private constructor(
    private val inbox: java.util.concurrent.BlockingQueue<ByteArray>,
    private val outbox: java.util.concurrent.BlockingQueue<ByteArray>,
) : PaymentLink {
    override fun send(data: ByteArray) {
        outbox.put(data)
    }

    override fun receive(timeoutMs: Long): ByteArray? = inbox.poll(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS)

    companion object {
        fun pair(): Pair<LoopbackLink, LoopbackLink> {
            val aToB = java.util.concurrent.LinkedBlockingQueue<ByteArray>()
            val bToA = java.util.concurrent.LinkedBlockingQueue<ByteArray>()
            return LoopbackLink(bToA, aToB) to LoopbackLink(aToB, bToA)
        }
    }
}

private fun crc(text: String): String = CRC32().also { it.update(text.toByteArray()) }.value.toString(16).padStart(8, '0')

private val b64 = Base64.getUrlEncoder().withoutPadding()

/** Split `payload` into text frames `WF1.<session>.<index>.<count>.<base64url>.<crc>` for an animated QR. */
fun encodeFrames(payload: ByteArray, session: ByteArray, chunkBytes: Int = DEFAULT_CHUNK_BYTES): List<String> {
    if (session.size != 4) throw TapException("The session id must be 4 bytes.")
    if (chunkBytes <= 0) throw TapException("The chunk size must be positive.")
    val chunks = if (payload.isEmpty()) listOf(ByteArray(0)) else payload.toList().chunked(chunkBytes).map { it.toByteArray() }
    if (chunks.size > 999) throw TapException("The payload needs too many frames.")
    return chunks.mapIndexed { index, chunk ->
        val head = "$FRAME_PREFIX.${session.toHex()}.$index.${chunks.size}.${b64.encodeToString(chunk)}"
        "$head.${crc(head)}"
    }
}

data class Progress(val received: Int, val total: Int, val missing: List<Int>) {
    val complete get() = missing.isEmpty()
}

/** Collects frames of one session. Duplicates are harmless; damaged or foreign frames throw [TapException]. */
class FrameAssembler {
    private var session: String? = null
    private var total: Int? = null
    private val chunks = mutableMapOf<Int, ByteArray>()

    fun add(frame: String): Progress {
        val parts = frame.trim().split(".")
        if (parts.size != 6 || parts[0] != FRAME_PREFIX) throw TapException("This is not a Wayfarer frame.")
        if (crc(parts.take(5).joinToString(".")) != parts[5]) throw TapException("A frame was damaged; scan it again.")
        val index = parts[2].takeIf { it.all(Char::isDigit) && it.isNotEmpty() }?.toIntOrNull()
        val count = parts[3].takeIf { it.all(Char::isDigit) && it.isNotEmpty() }?.toIntOrNull()
        if (index == null || count == null || count < 1 || index >= count) throw TapException("A frame header is invalid.")
        if (session == null) {
            session = parts[1]
            total = count
        } else if (parts[1] != session || count != total) {
            throw TapException("This frame belongs to a different transfer.")
        }
        val chunk = try {
            Base64.getUrlDecoder().decode(parts[4])
        } catch (e: IllegalArgumentException) {
            throw TapException("A frame body is invalid.")
        }
        val known = chunks[index]
        if (known != null && !known.contentEquals(chunk)) throw TapException("Two different frames claim the same position.")
        chunks[index] = chunk
        return progress()
    }

    fun progress(): Progress {
        val n = total ?: 0
        return Progress(chunks.size, n, (0 until n).filter { it !in chunks })
    }

    fun payload(): ByteArray {
        val n = total
        if (n == null || !progress().complete) throw TapException("Some frames are still missing.")
        return (0 until n).map { chunks.getValue(it) }.reduce { a, b -> a + b }
    }
}
