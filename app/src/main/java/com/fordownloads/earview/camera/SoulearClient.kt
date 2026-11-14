package com.fordownloads.earview.camera

import java.io.ByteArrayOutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * Soulear / i4season WiFi otoscope backend.
 *
 * Control on UDP 10005, open-video on UDP 10006, video returned to the source
 * port of the open-video command. One socket handles control and video.
 */
class SoulearClient(
    private val host: String,
    private val localIp: String,
    private val callback: CameraClientCallback,
) {

    @Volatile
    private var running = false

    @Volatile
    private var socket: DatagramSocket? = null

    @Volatile
    private var localPort = 0

    private var readerThread: Thread? = null
    private var controlThread: Thread? = null
    private var lampThread: Thread? = null

    private val sequenceLock = Any()
    private var sequenceCounter = Random.nextInt(1, 0x10000)
    private val pending = ConcurrentHashMap<Int, Pending>()
    private val assembler = SoulearAssembler()

    @Volatile
    private var lastVideo = 0L

    @Volatile
    private var connected = false

    @Volatile
    private var lampOn = false

    @Volatile
    private var licensed = false

    @Volatile
    private var model = "Soulear"

    private val clientId = Random.nextLong()

    fun start() {
        if (running) return
        val opened = openSocket()
        if (opened == null) {
            callback.onConnection(false)
            return
        }
        socket = opened
        localPort = opened.localPort
        lastVideo = 0L
        running = true
        callback.onModel(model)

        readerThread = Thread({ readerLoop() }, "soulear-reader").apply {
            isDaemon = true
            start()
        }
        controlThread = Thread({ controlLoop() }, "soulear-control").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running = false
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        readerThread?.let { join(it, 1200) }
        controlThread?.let { join(it, 1500) }
        lampThread?.let { join(it, 1200) }
        readerThread = null
        controlThread = null
        lampThread = null
        pending.clear()
    }

    fun toggleLamp() {
        if (!running) return
        if (lampThread?.isAlive == true) return
        lampThread = Thread({ setLed(!lampOn) }, "soulear-lamp").apply {
            isDaemon = true
            start()
        }
    }

    // ------------------------------------------------------------------
    // sockets / threads
    // ------------------------------------------------------------------

    private fun openSocket(): DatagramSocket? {
        for (port in intArrayOf(PIC_PORT, 0)) {
            try {
                val s = NetIf.openDatagram(localIp, port, 8 shl 20)
                s.soTimeout = 200
                return s
            } catch (_: Exception) {
            }
        }
        return null
    }

    private fun readerLoop() {
        while (running) {
            val s = socket ?: break
            val data = try {
                receive(s)
            } catch (_: SocketTimeoutException) {
                continue
            } catch (_: Exception) {
                break
            }

            if (data.size >= HEADER_SIZE && Protocol.u32(data, 0) == MAGIC) {
                val sequence = Protocol.u16(data, 4)
                val request = pending[sequence] ?: continue
                val status = data[9].toInt() and 0xFF
                val length = Protocol.u16(data, 10)
                val body = if (HEADER_SIZE + length <= data.size) {
                    data.copyOfRange(HEADER_SIZE, HEADER_SIZE + length)
                } else {
                    ByteArray(0)
                }
                request.complete(if (status == 0) body else null)
                continue
            }

            val jpeg = assembler.feed(data) ?: continue
            lastVideo = System.nanoTime()
            if (!connected) {
                connected = true
                callback.onConnection(true)
            }
            callback.onJpeg(jpeg)
        }
    }

    private fun controlLoop() {
        try {
            queryDevinfo()
            request(CMD_LICGET, attempts = 3, timeoutMs = 150)
            licensed = true
            var ledAvailable = queryLed()
            if (!ledAvailable) {
                sleep(400)
                ledAvailable = queryLed()
            }
            callback.onLampAvailability(ledAvailable)
        } catch (_: Exception) {
        }

        var lastOpen = 0L
        var lastDevInfo = System.currentTimeMillis()
        while (running) {
            val now = System.currentTimeMillis()
            if (System.nanoTime() - lastVideo > 1_000_000_000L && now - lastOpen > 1000) {
                openVideo()
                lastOpen = now
            }
            if (now - lastDevInfo >= 10000) {
                queryDevinfo()
                lastDevInfo = now
            }
            sleep(200)
        }
    }

    // ------------------------------------------------------------------
    // requests
    // ------------------------------------------------------------------

    private fun nextSequence(): Int = synchronized(sequenceLock) {
        sequenceCounter = (sequenceCounter + 1) and 0xFFFF
        sequenceCounter
    }

    private fun request(
        command: Int,
        payload: ByteArray = ByteArray(0),
        port: Int = CMD_PORT,
        attempts: Int = 8,
        timeoutMs: Long = 100,
    ): ByteArray? {
        val s = socket ?: return null
        val sequence = nextSequence()
        val holder = Pending()
        pending[sequence] = holder
        try {
            val packet = requestPacket(sequence, command, payload)
            val destination = try {
                InetSocketAddress(InetAddress.getByName(host), port)
            } catch (_: Exception) {
                return null
            }
            repeat(attempts) {
                if (!running) return null
                try {
                    s.send(DatagramPacket(packet, packet.size, destination))
                } catch (_: Exception) {
                    return null
                }
                try {
                    if (holder.latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                        return holder.value
                    }
                } catch (_: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return null
                }
            }
            return null
        } finally {
            pending.remove(sequence)
        }
    }

    private fun queryDevinfo() {
        val body = request(CMD_DEVINFO) ?: return
        val info = parseDevinfo(body) ?: return
        if (info.product.isNotEmpty()) {
            model = info.product
            callback.onModel(model)
        }
        callback.onBattery(info.battery, false)
    }

    private fun queryLed(): Boolean {
        val body = request(CMD_LED, byteArrayOf(1, 0, 0)) ?: return false
        if (body.size < 3) return false
        lampOn = body[1].toInt() != 0
        callback.onLamp(lampOn)
        return true
    }

    private fun setLed(on: Boolean) {
        val body = request(
            CMD_LED,
            byteArrayOf(0x11, if (on) 1 else 0, if (on) 100 else 0),
        ) ?: return
        if (body.size >= 3) {
            lampOn = body[1].toInt() != 0
            callback.onLamp(lampOn)
        }
    }

    private fun openVideo() {
        val payload = ByteArray(8)
        Protocol.putU16(payload, 0, localPort)
        Protocol.putU16(payload, 2, 0)
        Protocol.putU32(payload, 4, clientId and 0xFFFFFFFFL)
        request(CMD_OPEN_VIDEO, payload, VIDEO_CMD_PORT, attempts = 4)
        if (!licensed) {
            request(CMD_LICGET, attempts = 2, timeoutMs = 100)
            licensed = true
        }
    }

    // ------------------------------------------------------------------
    // frame reassembly
    // ------------------------------------------------------------------

    private class SoulearAssembler {
        private var frameId = -1
        private var lastSequence = -1
        private var broken = false
        private val chunks = ArrayList<ByteArray>()

        fun feed(packet: ByteArray): ByteArray? {
            if (packet.isEmpty()) return null
            val type = packet[0].toInt() and 0xFF
            val headerSize = when (type) {
                1 -> 16
                6 -> 28
                else -> return null
            }
            if (packet.size < headerSize) return null

            val sequence = packet[1].toInt() and 0xFF
            val id = packet[2].toInt() and 0xFF
            val last = (packet[3].toInt() and 0xFF) != 0
            val count = packet[4].toInt() and 0xFF

            if (id != frameId) {
                frameId = id
                chunks.clear()
                broken = false
            } else if (sequence != ((lastSequence + 1) and 0xFF)) {
                broken = true
            }
            lastSequence = sequence
            chunks.add(packet.copyOfRange(headerSize, packet.size))

            if (!last) return null

            val complete = !broken && chunks.size == count
            val data = ByteArrayOutputStream().also { output ->
                for (chunk in chunks) output.write(chunk)
            }.toByteArray()
            frameId = -1
            chunks.clear()
            broken = false

            if (!complete || data.size < 2) return null
            if (data[0] != 0xFF.toByte() || data[1] != 0xD8.toByte()) return null
            val end = Protocol.jpegEnd(data, 0)
            return if (end != null) data.copyOf(end) else data
        }
    }

    private class Pending {
        val latch = CountDownLatch(1)

        @Volatile
        var value: ByteArray? = null

        fun complete(result: ByteArray?) {
            value = result
            latch.countDown()
        }
    }

    companion object {
        const val HOST = "192.168.1.1"
        const val CMD_PORT = 10005
        const val VIDEO_CMD_PORT = 10006
        const val PIC_PORT = 40000

        private const val MAGIC = 0xFFEEFFEEL
        private const val HEADER_SIZE = 12

        const val CMD_DEVINFO = 0x0001
        const val CMD_LICGET = 0x0002
        const val CMD_OPEN_VIDEO = 0x0004
        const val CMD_LED = 0x000A

        /** Return the local IPv4 that can reach the camera, or null. */
        fun probe(host: String = HOST, localIp: String? = null, timeoutMs: Long = 600): String? {
            val prefix = host.substringBeforeLast('.') + "."
            val addresses = if (localIp != null) listOf(localIp) else NetIf.candidates(prefix)
            val destination = try {
                InetSocketAddress(InetAddress.getByName(host), CMD_PORT)
            } catch (_: Exception) {
                return null
            }
            for (address in addresses) {
                val s = try {
                    NetIf.openDatagram(address, 0, 64 * 1024)
                } catch (_: Exception) {
                    continue
                }
                try {
                    s.soTimeout = 150
                    val sequence = Random.nextInt(1, 0x10000)
                    val packet = requestPacket(sequence, CMD_DEVINFO, ByteArray(0))
                    val deadline = System.currentTimeMillis() + timeoutMs
                    while (System.currentTimeMillis() < deadline) {
                        try {
                            s.send(DatagramPacket(packet, packet.size, destination))
                        } catch (_: Exception) {
                            break
                        }
                        try {
                            val reply = receive(s)
                            if (parseReply(reply, sequence, CMD_DEVINFO) != null) return address
                        } catch (_: SocketTimeoutException) {
                        } catch (_: Exception) {
                            break
                        }
                    }
                } finally {
                    s.close()
                }
            }
            return null
        }

        private fun requestPacket(sequence: Int, command: Int, payload: ByteArray): ByteArray {
            val out = ByteArray(HEADER_SIZE + payload.size)
            Protocol.putU32(out, 0, MAGIC)
            Protocol.putU16(out, 4, sequence and 0xFFFF)
            Protocol.putU16(out, 6, command and 0xFFFF)
            out[8] = 1
            out[9] = 0
            Protocol.putU16(out, 10, payload.size)
            System.arraycopy(payload, 0, out, HEADER_SIZE, payload.size)
            return out
        }

        private fun parseReply(data: ByteArray, sequence: Int, command: Int): ByteArray? {
            if (data.size < HEADER_SIZE || Protocol.u32(data, 0) != MAGIC) return null
            if (Protocol.u16(data, 4) != (sequence and 0xFFFF)) return null
            if (Protocol.u16(data, 6) != (command and 0xFFFF)) return null
            val status = data[9].toInt() and 0xFF
            val length = Protocol.u16(data, 10)
            if (HEADER_SIZE + length > data.size) return null
            if (status != 0) return null
            return data.copyOfRange(HEADER_SIZE, HEADER_SIZE + length)
        }

        private fun parseDevinfo(payload: ByteArray): DevInfo? {
            if (payload.size < 0x80) return null
            val status = (payload[0x77].toInt() and 0xFF) or
                ((payload[0x78].toInt() and 0xFF) shl 8)
            return DevInfo(
                product = cString(payload, 0x21, 32),
                battery = (status shr 9).coerceIn(0, 100),
            )
        }

        private fun cString(payload: ByteArray, offset: Int, maxLength: Int): String {
            val limit = minOf(offset + maxLength, payload.size)
            var end = offset
            while (end < limit && payload[end] != 0.toByte()) end++
            return String(payload, offset, end - offset, Charsets.ISO_8859_1).trim()
        }

        private class DevInfo(val product: String, val battery: Int)
    }
}

private fun receive(socket: DatagramSocket): ByteArray {
    val buffer = ByteArray(65536)
    val packet = DatagramPacket(buffer, buffer.size)
    socket.receive(packet)
    return buffer.copyOf(packet.length)
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
