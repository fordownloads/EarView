package com.fordownloads.earview.camera

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper

/**
 * Detects the camera backend by UDP probing and owns the active client.
 * No SSID reading and no Wi-Fi auto-connect: the user joins the camera AP.
 */
class CameraManager(private val listener: Listener) {

    interface Listener {
        fun onFrame(bitmap: Bitmap)
        fun onConnection(connected: Boolean)
        fun onBattery(percent: Int, charging: Boolean)
        fun onModel(model: String)
        fun onLamp(on: Boolean)
        fun onLampAvailability(available: Boolean)
    }

    private val handler = Handler(Looper.getMainLooper())
    private val lock = Any()

    private var itimo: ItimoClient? = null
    private var soulear: SoulearClient? = null
    private var detectThread: Thread? = null

    @Volatile
    private var running = false

    private var generation = 0

    @Volatile
    private var framePostPending = false

    private val callback = object : CameraClientCallback {
        override fun onJpeg(jpeg: ByteArray) {
            if (!running || framePostPending) return
            val bitmap = try {
                BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
            } catch (_: Throwable) {
                null
            } ?: return
            framePostPending = true
            handler.post {
                framePostPending = false
                if (running) listener.onFrame(bitmap)
            }
        }

        override fun onConnection(connected: Boolean) {
            handler.post {
                if (running) listener.onConnection(connected)
            }
        }

        override fun onBattery(percent: Int, charging: Boolean) {
            handler.post {
                if (running) listener.onBattery(percent, charging)
            }
        }

        override fun onModel(model: String) {
            handler.post {
                if (running) listener.onModel(model)
            }
        }

        override fun onLamp(on: Boolean) {
            handler.post {
                if (running) listener.onLamp(on)
            }
        }

        override fun onLampAvailability(available: Boolean) {
            handler.post {
                if (running) listener.onLampAvailability(available)
            }
        }
    }

    fun start() {
        if (running) return
        running = true
        startDetection()
    }

    fun stop() {
        running = false
        generation++
        synchronized(lock) {
            itimo?.stop()
            itimo = null
            soulear?.stop()
            soulear = null
        }
        detectThread = null
    }

    fun retry() {
        if (!running) return
        generation++
        synchronized(lock) {
            itimo?.stop()
            itimo = null
            soulear?.stop()
            soulear = null
        }
        handler.post {
            if (running) listener.onConnection(false)
        }
        startDetection()
    }

    fun toggleLamp() {
        synchronized(lock) {
            itimo?.toggleLamp()
            soulear?.toggleLamp()
        }
    }

    // ------------------------------------------------------------------

    private fun startDetection() {
        val token = ++generation
        detectThread = Thread({
            val found = detectBackend()
            handler.post {
                if (!running || token != generation) return@post
                if (found == null) {
                    listener.onConnection(false)
                } else {
                    startClient(found.first, found.second)
                }
            }
        }, "earview-detect").apply {
            isDaemon = true
            start()
        }
    }

    private fun detectBackend(): Pair<String, String>? {
        if (!running) return null
        SoulearClient.probe(SoulearClient.HOST)?.let { return "soulear" to it }
        if (!running) return null
        ItimoClient.probe(ItimoClient.HOST)?.let { return "itimo" to it }
        return null
    }

    private fun startClient(backend: String, localIp: String) {
        synchronized(lock) {
            if (!running) return
            itimo?.stop()
            itimo = null
            soulear?.stop()
            soulear = null
            when (backend) {
                "soulear" -> SoulearClient(SoulearClient.HOST, localIp, callback).also {
                    soulear = it
                    it.start()
                }

                else -> ItimoClient(ItimoClient.HOST, localIp, callback).also {
                    itimo = it
                    it.start()
                }
            }
        }
    }
}
