package com.fordownloads.earview.camera

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import kotlin.math.roundToInt

/**
 * iTiMO / MZ otoscope backend (Pi Pico-style UDP camera).
 *
 * Commands: UDP [CMD_PORT]. Video: UDP [VIDEO_PORT], 24-byte fragment header.
 */
class ItimoClient(
    private val host: String,
    private val localIp: String,
    private val callback: CameraClientCallback,
) {

    @Volatile
    private var running = false
    private var videoThread: Thread? = null
    private var commandThread: Thread? = null
    private var lampThread: Thread? = null

    @Volatile
    private var lampOn = false

    fun start() {
        if (running) return
        running = true
        callback.onModel("iTiMO")
        callback.onLampAvailability(true)
        videoThread = Thread({ videoLoop() }, "itimo-video").apply {
            isDaemon = true
            start()
        }
        commandThread = Thread({ commandLoop() }, "itimo-command").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running = false
        videoThread?.let { join(it, 1500) }
        commandThread?.let { join(it, 1500) }
        lampThread?.let { join(it, 1500) }
        videoThread = null
        commandThread = null
        lampThread = null
    }

    fun toggleLamp() {
        if (!running) return
        if (lampThread?.isAlive == true) return
        lampThread = Thread({ lampWorker() }, "itimo-lamp").apply {
            isDaemon = true
            start()
        }
    }

    // ------------------------------------------------------------------
    // video
    // ------------------------------------------------------------------

    private fun videoLoop() {
        val startPacket = Protocol.cmdPacket(1, 0)
        val destination = try {
            InetSocketAddress(InetAddress.getByName(host), VIDEO_PORT)
        } catch (_: Exception) {
            return
        }
        while (running) {
            val socket = try {
                NetIf.openDatagram(localIp, 0, 8 shl 20)
            } catch (_: Exception) {
                sleep(500)
                continue
            }
            try {
                socket.soTimeout = 500
                val assembler = ItimoAssembler()
                var lastPacket = System.nanoTime()
                var lastSend = 0L
                var connected = false

                send(socket, startPacket, destination)
                lastSend = System.nanoTime()

                while (running) {
                    val now = System.nanoTime()
                    if (now - lastSend > 2_000_000_000L) {
                        send(socket, startPacket, destination)
                        lastSend = now
                    }
                    if (connected && now - lastPacket > 3_000_000_000L) {
                        connected = false
                        callback.onConnection(false)
                    }
                    val packet = try {
                        receive(socket)
                    } catch (_: SocketTimeoutException) {
                        continue
                    } catch (_: Exception) {
                        break
                    }
                    lastPacket = System.nanoTime()
                    val jpeg = assembler.feed(packet, lastPacket) ?: continue
                    if (!connected) {
                        connected = true
                        callback.onConnection(true)
                    }
                    callback.onJpeg(jpeg)
                }
            } finally {
                socket.close()
            }
        }
    }

    // ------------------------------------------------------------------
    // commands
    // ------------------------------------------------------------------

    private fun commandLoop() {
        val socket = try {
            NetIf.openDatagram(localIp, 0, 1 shl 20)
        } catch (_: Exception) {
            return
        }
        try {
            socket.soTimeout = 550
            var lastBattery = 0L
            var lastModel = 0L
            while (running) {
                val now = System.currentTimeMillis()
                if (now - lastBattery >= 5000) {
                    queryBattery(socket)
                    lastBattery = now
                }
                if (now - lastModel >= 20000) {
                    queryModel(socket)
                    lastModel = now
                }
                sleep(250)
            }
        } finally {
            socket.close()
        }
    }

    private fun queryBattery(socket: DatagramSocket) {
        val reply = sendCommand(socket, CMD_GET_BATTERY) ?: return
        if (reply.size < 12) return
        val value = Protocol.u32(reply, 8)
        callback.onBattery(batteryPercent(value), ((value shr 16) and 1L) != 0L)
    }

    private fun queryModel(socket: DatagramSocket) {
        val reply = sendCommand(socket, CMD_GET_BOARD_INFO) ?: return
        val text = decodeBoardInfo(reply) ?: return
        callback.onModel(text)
    }

    private fun lampWorker() {
        val socket = try {
            NetIf.openDatagram(localIp, 0, 1 shl 20)
        } catch (_: Exception) {
            return
        }
        try {
            socket.soTimeout = 550
            if (sendCommand(socket, CMD_SET_LAMP) != null) {
                lampOn = !lampOn
                callback.onLamp(lampOn)
            }
        } finally {
            socket.close()
        }
    }

    private fun sendCommand(
        socket: DatagramSocket,
        command: Int,
        payload: ByteArray = ByteArray(0),
        tries: Int = 2,
        timeoutMs: Int = 550,
    ): ByteArray? {
        val sequence = (System.currentTimeMillis() / 1000L) and 0xFFFFFFFFL
        val packet = Protocol.cmdPacket(command, sequence, payload)
        val destination = try {
            InetSocketAddress(InetAddress.getByName(host), CMD_PORT)
        } catch (_: Exception) {
            return null
        }
        repeat(tries) {
            if (!running) return null
            try {
                socket.send(DatagramPacket(packet, packet.size, destination))
            } catch (_: Exception) {
                return null
            }
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline && running) {
                try {
                    val data = receive(socket)
                    if (data.size >= 4 &&
                        data[0] == packet[0] && data[1] == packet[1] &&
                        data[2] == packet[2] && data[3] == packet[3]
                    ) {
                        return data
                    }
                } catch (_: SocketTimeoutException) {
                    // keep trying until the deadline
                } catch (_: Exception) {
                    return null
                }
            }
        }
        return null
    }

    // ------------------------------------------------------------------
    // frame reassembly
    // ------------------------------------------------------------------

    private class ItimoFrame(val total: Int) {
        val parts = HashMap<Int, ByteArray>()
        var ended = false
        var need = 0
        var updated = 0L
    }

    private class ItimoAssembler {
        private val frames = HashMap<Int, ItimoFrame>()
        private var lastFrameId: Int? = null

        fun feed(packet: ByteArray, now: Long): ByteArray? {
            if (packet.size < 24 || packet[0].toInt() != 0x66) return null

            val type = packet[1].toInt() and 0xFF
            val frameId = packet[3].toInt() and 0xFF
            val total = Protocol.u32(packet, 4).toInt()
            val sequence = Protocol.u16(packet, 12)
            val payloadLength = Protocol.u16(packet, 14)
            if (payloadLength <= 0 || 24 + payloadLength > packet.size) return null
            val payload = packet.copyOfRange(24, 24 + payloadLength)

            val frame: ItimoFrame?
            if (type == 1) {
                if (total > (4 shl 20) || sequence != 0) {
                    frames.remove(frameId)
                    return null
                }
                val created = ItimoFrame(total)
                created.parts[0] = payload
                created.updated = now
                frames[frameId] = created
                if (frames.size > 8) {
                    val oldest = frames.entries
                        .filter { it.key != frameId }
                        .minByOrNull { it.value.updated } ?: null
                    oldest?.let { frames.remove(it.key) }
                }
                frame = created
            } else {
                frame = frames[frameId] ?: return null
                frame.updated = now
                frame.parts.putIfAbsent(sequence, payload)
                if (type == 2) {
                    frame.ended = true
                    frame.need = sequence + 1
                }
            }

            if (!frame.ended) return null
            for (i in 0 until frame.need) {
                if (!frame.parts.containsKey(i)) return null
            }

            val data = ByteArrayOutputStream()
            for (i in 0 until frame.need) {
                frame.parts[i]?.let { data.write(it) }
            }
            frames.remove(frameId)

            val assembled = data.toByteArray()
            val frameBytes = trimFrame(assembled, frame.total) ?: return null

            val previous = lastFrameId
            if (previous != null) {
                val delta = (frameId - previous) and 0xFF
                if (delta == 0 || delta >= 128) return null
            }
            lastFrameId = frameId
            return frameBytes
        }

        private fun trimFrame(data: ByteArray, total: Int): ByteArray? {
            val trimmed = if (data.size > total) data.copyOf(total) else data
            if (trimmed.size >= 3 &&
                trimmed[0] == 0xFF.toByte() &&
                trimmed[1] == 0xD8.toByte() &&
                trimmed[2] == 0xFF.toByte()
            ) {
                val end = Protocol.jpegEnd(trimmed, 0)
                if (end != null) return trimmed.copyOf(end)
            }
            // Fallback: find the first complete JPEG inside the buffer.
            var start = 0
            while (start + 2 < trimmed.size) {
                if (trimmed[start] == 0xFF.toByte() &&
                    trimmed[start + 1] == 0xD8.toByte() &&
                    trimmed[start + 2] == 0xFF.toByte()
                ) {
                    val end = Protocol.jpegEnd(trimmed, start)
                    if (end != null) return trimmed.copyOfRange(start, end)
                }
                start++
            }
            return null
        }
    }

    companion object {
        const val HOST = "192.168.10.123"
        const val VIDEO_PORT = 8031
        const val CMD_PORT = 50000

        const val CMD_GET_BATTERY = 0x1017
        const val CMD_SET_LAMP = 0x1050
        const val CMD_GET_BOARD_INFO = 0x1060

        /** Return the local IPv4 that can reach the camera, or null. */
        fun probe(host: String = HOST, localIp: String? = null, timeoutMs: Int = 1000): String? {
            val prefix = host.substringBeforeLast('.') + "."
            val addresses = if (localIp != null) listOf(localIp) else NetIf.candidates(prefix)
            val packet = Protocol.cmdPacket(CMD_GET_BATTERY, 1)
            val destination = try {
                InetSocketAddress(InetAddress.getByName(host), CMD_PORT)
            } catch (_: Exception) {
                return null
            }
            for (address in addresses) {
                val socket = try {
                    NetIf.openDatagram(address, 0)
                } catch (_: Exception) {
                    continue
                }
                try {
                    socket.soTimeout = 150
                    val deadline = System.currentTimeMillis() + timeoutMs
                    while (System.currentTimeMillis() < deadline) {
                        try {
                            socket.send(DatagramPacket(packet, packet.size, destination))
                        } catch (_: Exception) {
                            break
                        }
                        try {
                            val reply = receive(socket)
                            if (reply.size >= 4 &&
                                reply[0] == packet[0] && reply[1] == packet[1] &&
                                reply[2] == packet[2] && reply[3] == packet[3]
                            ) {
                                return address
                            }
                        } catch (_: SocketTimeoutException) {
                            // try again until the deadline
                        } catch (_: Exception) {
                            break
                        }
                    }
                } finally {
                    socket.close()
                }
            }
            return null
        }

        /** Port of the app's battery ADC -> percent mapping (low 16 bits). */
        fun batteryPercent(value: Long): Int {
            val raw = (value and 0xFFFF).toInt()
            val percent = when {
                raw >= 0xFD2 -> 100.0
                raw >= 0xE74 -> 50.0 + (raw - 0xE74) * 0.14
                raw >= 0xDC0 -> 20.0 + (raw - 0xDCA) * 0.165
                raw >= 0xD7A -> 10.0 + (raw - 0xD7A) * 0.14
                raw >= 0xD3E -> 1.0 + (raw - 0xD3E) * 0.15
                else -> 0.0
            }
            return percent.roundToInt().coerceIn(0, 100)
        }

        private fun decodeBoardInfo(reply: ByteArray): String? {
            if (reply.size < 24) return null
            val length = Protocol.u32(reply, 12).toInt()
            if (length !in 1..8192 || reply.size < 24 + length) return null
            val payload = reply.copyOfRange(24, 24 + length)
            var text = String(payload, Charsets.UTF_8).trim('\u0000').trim()
            if (text.isEmpty()) {
                val printable = payload.filter { it in 32.toByte()..126.toByte() }.toByteArray()
                text = String(printable, Charsets.US_ASCII).trim()
            }
            if (text.isEmpty()) return null

            val checksum = (Protocol.u32(reply, 8) and 0xFF).toInt()
            var xor = 0
            for (byte in payload) xor = xor xor (byte.toInt() and 0xFF)
            if (xor != checksum && text.length < 2) return null

            // Newer firmware returns a JSON board-info document.
            try {
                val json = JSONObject(text)
                for (key in arrayOf("model", "brand", "name", "ssid_head")) {
                    val value = json.optString(key, "")
                    if (value.isNotEmpty()) return value.trim()
                }
            } catch (_: Exception) {
            }
            return text
        }
    }
}

private fun receive(socket: DatagramSocket): ByteArray {
    val buffer = ByteArray(65536)
    val packet = DatagramPacket(buffer, buffer.size)
    socket.receive(packet)
    return buffer.copyOf(packet.length)
}

private fun send(socket: DatagramSocket, data: ByteArray, destination: InetSocketAddress) {
    try {
        socket.send(DatagramPacket(data, data.size, destination))
    } catch (_: Exception) {
    }
}

private fun sleep(milliseconds: Long) {
    try {
        Thread.sleep(milliseconds)
    } catch (_: InterruptedException) {
        Thread.currentThread().interrupt()
    }
}

private fun join(thread: Thread, timeoutMs: Long) {
    try {
        thread.join(timeoutMs)
    } catch (_: InterruptedException) {
        Thread.currentThread().interrupt()
    }
}
