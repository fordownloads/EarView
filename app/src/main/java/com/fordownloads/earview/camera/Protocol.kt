package com.fordownloads.earview.camera

/** Wire-level helpers shared by both camera backends. */
object Protocol {

    const val ITIMO_MAGIC = 0x9999

    /** 24-byte iTiMO command packet; payload starts at offset 8. */
    fun cmdPacket(cmd: Int, seq: Long, payload: ByteArray = ByteArray(0)): ByteArray {
        val out = ByteArray(24)
        putU16(out, 0, ITIMO_MAGIC)
        putU16(out, 2, cmd)
        putU32(out, 4, seq)
        System.arraycopy(payload, 0, out, 8, minOf(payload.size, 16))
        return out
    }

    fun putU16(buf: ByteArray, offset: Int, value: Int) {
        buf[offset] = (value and 0xFF).toByte()
        buf[offset + 1] = ((value ushr 8) and 0xFF).toByte()
    }

    fun putU32(buf: ByteArray, offset: Int, value: Long) {
        buf[offset] = (value and 0xFF).toByte()
        buf[offset + 1] = ((value ushr 8) and 0xFF).toByte()
        buf[offset + 2] = ((value ushr 16) and 0xFF).toByte()
        buf[offset + 3] = ((value ushr 24) and 0xFF).toByte()
    }

    fun u16(buf: ByteArray, offset: Int): Int =
        (buf[offset].toInt() and 0xFF) or ((buf[offset + 1].toInt() and 0xFF) shl 8)

    fun u32(buf: ByteArray, offset: Int): Long =
        (buf[offset].toLong() and 0xFF) or
            ((buf[offset + 1].toLong() and 0xFF) shl 8) or
            ((buf[offset + 2].toLong() and 0xFF) shl 16) or
            ((buf[offset + 3].toLong() and 0xFF) shl 24)

    /** Index just past the JPEG EOI that starts at [start], or null if incomplete. */
    fun jpegEnd(buf: ByteArray, start: Int = 0): Int? {
        val n = buf.size
        if (start + 2 > n) return null
        if (buf[start] != 0xFF.toByte() || buf[start + 1] != 0xD8.toByte()) return null
        var i = start + 2
        while (i + 1 < n) {
            if (buf[i] != 0xFF.toByte()) {
                var j = i
                while (j < n && buf[j] != 0xFF.toByte()) j++
                if (j >= n) return null
                i = j
                continue
            }
            while (i < n && buf[i] == 0xFF.toByte()) i++
            if (i >= n) return null
            val marker = buf[i].toInt() and 0xFF
            i++
            if (marker == 0xD9) return i
            if (marker == 0xD8) return null
            if (marker in 0xD0..0xD7 || marker == 0x01 || marker == 0x00) continue
            if (i + 2 > n) return null
            val segment = ((buf[i].toInt() and 0xFF) shl 8) or (buf[i + 1].toInt() and 0xFF)
            if (segment < 2) return null
            i += segment
        }
        return null
    }
}
