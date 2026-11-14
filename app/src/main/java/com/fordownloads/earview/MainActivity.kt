package com.fordownloads.earview

import android.app.Activity
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.TextView
import com.fordownloads.earview.camera.CameraManager
import com.fordownloads.earview.ui.VideoView
import java.util.Locale

class MainActivity : Activity(), CameraManager.Listener {

    private lateinit var prefs: Prefs
    private lateinit var videoView: VideoView
    private lateinit var batteryIcon: ImageView
    private lateinit var batteryText: TextView
    private lateinit var modelText: TextView
    private lateinit var fpsText: TextView
    private lateinit var rotateButton: ImageButton
    private lateinit var mirrorButton: ImageButton
    private lateinit var lampButton: ImageButton
    private lateinit var noDevice: View
    private lateinit var retryButton: TextView

    private var manager: CameraManager? = null
    private var connected = false
    private var searching = false
    private var lampAvailable = true
    private var mountOffset = 0
    private var model = ""
    private var frames = 0

    private val handler = Handler(Looper.getMainLooper())

    private val fpsTick = object : Runnable {
        override fun run() {
            fpsText.text = getString(R.string.fps_format, frames)
            frames = 0
            handler.postDelayed(this, 1000)
        }
    }

    private val retryReset = Runnable {
        if (!connected) {
            searching = false
            retryButton.isEnabled = true
            retryButton.text = getString(R.string.retry)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Edge-to-edge is enforced for targetSdk 35: pad the content out of the
        // system bars and display cutout instead of drawing underneath them.
        findViewById<View>(R.id.root).setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(
                WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout()
            )
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }

        prefs = Prefs(this)

        videoView = findViewById(R.id.video)
        batteryIcon = findViewById(R.id.battery_icon)
        batteryText = findViewById(R.id.battery_text)
        modelText = findViewById(R.id.model_text)
        fpsText = findViewById(R.id.fps_text)
        rotateButton = findViewById(R.id.rotate_button)
        mirrorButton = findViewById(R.id.mirror_button)
        lampButton = findViewById(R.id.lamp_button)
        noDevice = findViewById(R.id.no_device)
        retryButton = findViewById(R.id.retry_button)

        videoView.listener = object : VideoView.Listener {
            override fun onMirrorToggle() {
                // The view already completed its flip animation.
                prefs.mirror = !prefs.mirror
                updateMirrorButton()
            }

            override fun onRotationCommit(baseDegrees: Int) {
                prefs.rotation = ((baseDegrees - mountOffset) % 360 + 360) % 360
            }
        }
        videoView.setMirror(prefs.mirror, false)
        videoView.setBaseRotation(prefs.rotation + mountOffset, false)

        rotateButton.setOnClickListener { rotateBy90() }
        mirrorButton.setOnClickListener { toggleMirror() }
        lampButton.setOnClickListener { manager?.toggleLamp() }
        retryButton.setOnClickListener { retry() }

        updateMirrorButton()
        updateBattery(-1)
        updateConnected(false)
    }

    override fun onStart() {
        super.onStart()
        manager = CameraManager(this).also { it.start() }
        handler.removeCallbacks(fpsTick)
        handler.post(fpsTick)
    }

    override fun onResume() {
        super.onResume()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onPause() {
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        super.onPause()
    }

    override fun onStop() {
        handler.removeCallbacks(fpsTick)
        handler.removeCallbacks(retryReset)
        manager?.stop()
        manager = null
        connected = false
        super.onStop()
    }

    // ------------------------------------------------------------------
    // camera callbacks (main thread)
    // ------------------------------------------------------------------

    override fun onFrame(bitmap: Bitmap) {
        videoView.setBitmap(bitmap)
        if (!connected) updateConnected(true)
        frames++
    }

    override fun onConnection(connected: Boolean) {
        updateConnected(connected)
    }

    override fun onBattery(percent: Int, charging: Boolean) {
        updateBattery(percent)
    }

    override fun onModel(model: String) {
        this.model = model
        val upper = model.uppercase(Locale.ROOT)
        val offset = if (upper.contains("FBPRO") || upper.contains("R1")) 180 else 0
        if (offset != mountOffset) {
            mountOffset = offset
            videoView.setBaseRotation(prefs.rotation + mountOffset)
        }
        if (connected) modelText.text = model
    }

    override fun onLamp(on: Boolean) {
        lampButton.setImageResource(if (on) R.drawable.ic_lamp_on else R.drawable.ic_lamp_off)
        lampButton.imageTintList = ColorStateList.valueOf(
            getColor(if (on) R.color.lamp_active else R.color.white)
        )
    }

    override fun onLampAvailability(available: Boolean) {
        lampAvailable = available
        lampButton.visibility = if (available) View.VISIBLE else View.GONE
    }

    // ------------------------------------------------------------------
    // UI state
    // ------------------------------------------------------------------

    private fun updateConnected(value: Boolean) {
        connected = value
        if (value) {
            searching = false
            handler.removeCallbacks(retryReset)
            noDevice.visibility = View.GONE
            rotateButton.isEnabled = true
            mirrorButton.isEnabled = true
            lampButton.isEnabled = true
            lampButton.visibility = if (lampAvailable) View.VISIBLE else View.GONE
            rotateButton.alpha = 1f
            mirrorButton.alpha = 1f
            lampButton.alpha = 1f
            modelText.text = model
            retryButton.isEnabled = true
            retryButton.text = getString(R.string.retry)
        } else {
            noDevice.visibility = View.VISIBLE
            rotateButton.isEnabled = false
            mirrorButton.isEnabled = false
            lampButton.isEnabled = false
            rotateButton.alpha = 0.35f
            mirrorButton.alpha = 0.35f
            lampButton.alpha = 0.35f
            modelText.text = getString(R.string.model_unknown)
            fpsText.text = getString(R.string.fps_zero)
            frames = 0
            updateBattery(-1)
            if (searching) {
                retryButton.isEnabled = false
                retryButton.text = getString(R.string.searching)
            } else {
                retryButton.isEnabled = true
                retryButton.text = getString(R.string.retry)
            }
        }
    }

    private fun updateBattery(percent: Int) {
        if (percent < 0) {
            batteryText.text = getString(R.string.battery_unknown)
            batteryText.setTextColor(getColor(R.color.text_secondary))
            batteryIcon.imageTintList = ColorStateList.valueOf(getColor(R.color.battery_unknown))
            return
        }
        val color = when {
            percent >= 50 -> R.color.battery_ok
            percent >= 20 -> R.color.battery_low
            else -> R.color.battery_critical
        }
        batteryText.text = getString(R.string.percent_format, percent)
        batteryText.setTextColor(getColor(color))
        batteryIcon.imageTintList = ColorStateList.valueOf(getColor(color))
    }

    private fun updateMirrorButton() {
        mirrorButton.imageTintList = ColorStateList.valueOf(
            getColor(if (prefs.mirror) R.color.white else R.color.text_secondary)
        )
    }

    // ------------------------------------------------------------------
    // actions
    // ------------------------------------------------------------------

    private fun retry() {
        searching = true
        retryButton.isEnabled = false
        retryButton.text = getString(R.string.searching)
        handler.removeCallbacks(retryReset)
        manager?.retry()
        handler.postDelayed(retryReset, 5000)
    }

    private fun rotateBy90() {
        val next = (prefs.rotation + 90) % 360
        prefs.rotation = next
        videoView.setBaseRotation(next + mountOffset)
    }

    private fun toggleMirror() {
        val next = !prefs.mirror
        prefs.mirror = next
        videoView.setMirror(next)
        updateMirrorButton()
    }
}
