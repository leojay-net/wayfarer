package com.wayfarer.tap

import java.io.ByteArrayOutputStream
import java.math.BigInteger

/** A refusal with a reason that is safe to show to the user. Mirrors `TapError` in the Python package. */
class TapException(message: String) : Exception(message)

const val MAX_STRING = 256

/** Canonical binary encoding shared by every tap message: u32-length-prefixed UTF-8, little-endian integers. */
class Writer {
    private val out = ByteArrayOutputStream()

    fun raw(data: ByteArray) = apply { out.write(data) }

    fun u8(value: Int) = apply { out.write(value and 0xff) }

    fun u32(value: Long) = apply { for (i in 0 until 4) out.write(((value shr (8 * i)) and 0xff).toInt()) }

    fun u64(value: Long) = apply { for (i in 0 until 8) out.write(((value shr (8 * i)) and 0xff).toInt()) }

    fun u128(value: BigInteger) = apply {
        if (value.signum() < 0 || value.bitLength() > 128) throw TapException("An amount does not fit in 128 bits.")
        for (i in 0 until 16) out.write(value.shiftRight(8 * i).toInt() and 0xff)
    }

    fun string(value: String) = apply {
        val bytes = value.toByteArray(Charsets.UTF_8)
        if (bytes.size > MAX_STRING) throw TapException("A text field is too long.")
        u32(bytes.size.toLong())
        raw(bytes)
    }

    fun toByteArray(): ByteArray = out.toByteArray()
}

/** Strict decoder: rejects short input, oversize strings and trailing bytes. */
class Reader(private val data: ByteArray) {
    private var pos = 0

    fun raw(count: Int): ByteArray {
        if (count < 0 || pos + count > data.size) throw TapException("The message is truncated.")
        return data.copyOfRange(pos, pos + count).also { pos += count }
    }

    fun u8(): Int = raw(1)[0].toInt() and 0xff

    fun u32(): Long = (0 until 4).fold(0L) { acc, i -> acc or ((raw(1)[0].toLong() and 0xff) shl (8 * i)) }

    /** A u64 as a non-negative Long; values of 2^63 or more are refused (no real timestamp or amount needs them). */
    fun u64(): Long {
        val value = (0 until 8).fold(0L) { acc, i -> acc or ((raw(1)[0].toLong() and 0xff) shl (8 * i)) }
        if (value < 0) throw TapException("A number is out of range.")
        return value
    }

    fun u128(): BigInteger = BigInteger(1, raw(16).reversedArray())

    fun string(): String {
        val length = u32()
        if (length > MAX_STRING) throw TapException("A text field is too long.")
        val bytes = raw(length.toInt())
        return try {
            Charsets.UTF_8.newDecoder().decode(java.nio.ByteBuffer.wrap(bytes)).toString()
        } catch (e: java.nio.charset.CharacterCodingException) {
            throw TapException("A text field is not valid UTF-8.")
        }
    }

    fun finish() {
        if (pos != data.size) throw TapException("The message has unexpected trailing bytes.")
    }
}
