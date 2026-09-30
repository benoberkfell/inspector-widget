/*
 * ViewSpector — payload core module.
 *
 * Wire framing for the host <-> payload socket. Models on the real UI-Inspector
 * FramingProtocol:
 *   android-sources/tools-base/ui-inspector/common/src/main/java/
 *     com/android/tools/ui/inspector/common/FramingProtocol.kt:32-60
 * with the ViewSpector magic ("VWSPCT01") and the framing defined by
 * CONTRACT.md §4:
 *
 *   MAGIC(8 bytes ascii "VWSPCT01") + LEN(4 bytes BE uint32) + payload(LEN bytes)
 *
 * One Request is in flight at a time; the Response mirrors it back (mirrors the
 * UI-Inspector CommandSender / SessionHandler synchronous loop). A magic
 * mismatch is a hard error.
 */
package com.oberkfell.viewspector.agent.payload

import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream

/**
 * Reads and writes length-prefixed, magic-tagged messages.
 *
 * Symmetric with the Python host's framing in `inspector_widget.framing`. Both sides
 * agree on [MAGIC] and a 4-byte big-endian length.
 */
object Framing {

    /** ASCII magic prefixing every frame. CONTRACT.md §4. */
    const val MAGIC: String = "VWSPCT01"

    // US-ASCII bytes of the magic. 8 bytes by construction.
    private val MAGIC_BYTES: ByteArray = MAGIC.toByteArray(Charsets.US_ASCII)

    private const val MAGIC_SIZE: Int = 8

    // Payloads up to this size are read into one exact buffer; larger ones grow a
    // buffer as bytes actually arrive, so a LEN the peer never delivers costs nothing.
    private const val READ_CHUNK: Int = 64 * 1024

    init {
        // Guard the invariant the wire format depends on. If the constant is
        // ever edited to something other than 8 ASCII bytes this fails loudly
        // at class-load rather than silently corrupting frames.
        require(MAGIC_BYTES.size == MAGIC_SIZE) {
            "Framing MAGIC must be exactly $MAGIC_SIZE bytes, got ${MAGIC_BYTES.size}"
        }
    }

    /**
     * Reads exactly one framed message from [ins] and returns its payload bytes.
     *
     * Reads 8 magic bytes (validated against [MAGIC_BYTES]), 4 big-endian length
     * bytes, then exactly that many payload bytes (looping until satisfied —
     * [DataInputStream.readFully] does the loop for us). Throws on magic
     * mismatch, on a length over [maxLength] ([FrameTooLargeException], raised
     * before anything is allocated; CONTRACT.md §4), or on EOF mid-frame.
     *
     * Mirrors FramingProtocol.readMessage (FramingProtocol.kt:48-59).
     */
    fun readMessage(ins: InputStream, maxLength: Int = WireLimits.MAX_REQUEST_BYTES): ByteArray {
        val data = DataInputStream(ins)

        val magic = ByteArray(MAGIC_SIZE)
        data.readFully(magic) // loops; throws EOFException if the stream ends early
        if (!magic.contentEquals(MAGIC_BYTES)) {
            throw IllegalStateException(
                "Framing magic mismatch: expected \"$MAGIC\", got ${magic.toAsciiDebug()}"
            )
        }
        return readPayload(data, maxLength)
    }

    /**
     * A frame whose LEN is over the reader's limit. Thrown BEFORE anything is allocated
     * for the payload: the stream is then out of step, so the connection must be dropped.
     */
    class FrameTooLargeException(val length: Long, val limit: Int) :
        IllegalStateException("Framing length $length exceeds the $limit-byte limit")

    /**
     * Reads the 4-byte big-endian LEN (as the unsigned 32-bit value the contract defines)
     * and then exactly LEN payload bytes. Rejects LEN > [maxLength] before allocating;
     * a LEN above [READ_CHUNK] is read in chunks so memory follows the bytes received.
     */
    private fun readPayload(data: DataInputStream, maxLength: Int): ByteArray {
        val length = data.readInt().toLong() and 0xFFFFFFFFL // BE uint32
        if (length > maxLength) throw FrameTooLargeException(length, maxLength)
        val n = length.toInt()
        if (n <= READ_CHUNK) {
            val payload = ByteArray(n)
            if (n > 0) data.readFully(payload) // loops until all `n` bytes are read
            return payload
        }
        val out = ByteArrayOutputStream(READ_CHUNK)
        val buf = ByteArray(READ_CHUNK)
        var remaining = n
        while (remaining > 0) {
            val got = data.read(buf, 0, minOf(remaining, READ_CHUNK))
            if (got < 0) throw EOFException("EOF after ${n - remaining} of $n payload bytes")
            out.write(buf, 0, got)
            remaining -= got
        }
        return out.toByteArray()
    }

    /**
     * Writes one framed message to [out]: magic, then the big-endian length of
     * [payload], then the payload bytes; then flushes.
     *
     * Synchronized on [out] so concurrent writers (there should be at most one
     * per connection, but be defensive) never interleave partial frames.
     * Mirrors FramingProtocol.writeMessage (FramingProtocol.kt:38-46).
     */
    fun writeMessage(out: OutputStream, payload: ByteArray) {
        synchronized(out) {
            val data = DataOutputStream(out)
            data.write(MAGIC_BYTES)
            data.writeInt(payload.size) // big-endian
            if (payload.isNotEmpty()) {
                data.write(payload)
            }
            data.flush()
        }
    }

    /**
     * Like [readMessage] but returns `null` on a clean end-of-stream BEFORE any
     * bytes of a new frame have been read (i.e. the peer closed the connection
     * between messages). A mid-frame EOF still propagates as an exception, since
     * that indicates a torn frame rather than an orderly shutdown.
     *
     * The [Server] read loop uses this to distinguish "client hung up cleanly"
     * from "framing error".
     */
    fun readMessageOrNull(ins: InputStream, maxLength: Int = WireLimits.MAX_REQUEST_BYTES): ByteArray? {
        val data = DataInputStream(ins)

        val magic = ByteArray(MAGIC_SIZE)
        val first = data.read()
        if (first == -1) {
            // Clean EOF at a frame boundary: no more requests on this connection.
            return null
        }
        magic[0] = first.toByte()
        // Read the remaining 7 magic bytes; a short read here is a torn frame.
        data.readFully(magic, 1, MAGIC_SIZE - 1)
        if (!magic.contentEquals(MAGIC_BYTES)) {
            throw IllegalStateException(
                "Framing magic mismatch: expected \"$MAGIC\", got ${magic.toAsciiDebug()}"
            )
        }

        return readPayload(data, maxLength)
    }

    /** Human-readable rendering of magic bytes for error messages. */
    private fun ByteArray.toAsciiDebug(): String =
        joinToString(prefix = "[", postfix = "]") { b ->
            val c = b.toInt() and 0xFF
            if (c in 0x20..0x7E) c.toChar().toString() else "0x%02X".format(c)
        }
}
