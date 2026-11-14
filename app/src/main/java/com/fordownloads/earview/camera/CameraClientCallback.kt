package com.fordownloads.earview.camera

/** Callbacks fired from the camera worker threads. */
interface CameraClientCallback {
    fun onJpeg(jpeg: ByteArray)
    fun onConnection(connected: Boolean)
    fun onBattery(percent: Int, charging: Boolean)
    fun onModel(model: String)
    fun onLamp(on: Boolean)
    fun onLampAvailability(available: Boolean)
}
