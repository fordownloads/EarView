package com.fordownloads.earview

import android.app.Activity
import android.content.Intent
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import com.fordownloads.earview.camera.CameraManager
import com.fordownloads.earview.ui.BatteryView
import com.fordownloads.earview.ui.VideoView
import java.util.Locale
import kotlin.math.roundToInt

class MainActivity : Activity(), CameraManager.Listener {

    private lateinit var prefs: Prefs
    private lateinit var videoView: VideoView
    private lateinit var batteryView: BatteryView
    private lateinit var modelText: TextView
    private lateinit var fpsText: TextView
    private lateinit var topBar: LinearLayout
    private lateinit var videoArea: FrameLayout
    private lateinit var bottomBar: LinearLayout
    private lateinit var buttonPanel: LinearLayout
    private lateinit var rotateButton: ImageButton
    private lateinit var mirrorButton: ImageButton
    private lateinit var lampButton: ImageButton
    private lateinit var noDevice: View
    private lateinit var retryButton: TextView
    private lateinit var connectButton: TextView

    private var manager: CameraManager? = null
    private var connected = false
    private var searching = false
    private var lampAvailable = true
    private var mountOffset = 0
    private var model = ""
    private var frames = 0
    private var wifiPanelOpened = false

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
        batteryView = findViewById(R.id.battery_view)
        modelText = findViewById(R.id.model_text)
        fpsText = findViewById(R.id.fps_text)
        topBar = findViewById(R.id.top_bar)
        videoArea = findViewById(R.id.video_area)
        bottomBar = findViewById(R.id.bottom_bar)
        buttonPanel = findViewById(R.id.button_panel)
        rotateButton = findViewById(R.id.rotate_button)
        mirrorButton = findViewById(R.id.mirror_button)
        lampButton = findViewById(R.id.lamp_button)
        noDevice = findViewById(R.id.no_device)
        retryButton = findViewById(R.id.retry_button)
        connectButton = findViewById(R.id.connect_button)

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
        connectButton.setOnClickListener { openWifiPanel() }

        updateMirrorButton()
        updateBattery(-1)
        updateConnected(false)
        applyOrientation(resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE)
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applyOrientation(newConfig.orientation == Configuration.ORIENTATION_LANDSCAPE)
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
        if (wifiPanelOpened) {
            wifiPanelOpened = false
            if (!connected) {
                searching = true
                retryButton.isEnabled = false
                retryButton.text = getString(R.string.searching)
                manager?.retry()
                handler.removeCallbacks(retryReset)
                handler.postDelayed(retryReset, 5000)
            }
        }
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
        updateBattery(percent, charging)
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
            modelText.text = ""
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

    private fun updateBattery(percent: Int, charging: Boolean = false) {
        batteryView.setValue(percent, charging)
    }

    private fun updateMirrorButton() {
        mirrorButton.imageTintList = ColorStateList.valueOf(
            getColor(if (prefs.mirror) R.color.white else R.color.text_secondary)
        )
    }

    /**
     * Portrait: bars above and below the video.
     * Landscape: translucent info panel in the top-left and the buttons
     * centered on the right, both floating over the full-screen video.
     */
    private fun applyOrientation(landscape: Boolean) {
        if (landscape) {
            videoArea.layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            )

            topBar.orientation = LinearLayout.VERTICAL
            topBar.gravity = Gravity.TOP
            topBar.setPadding(dp(12), dp(12), dp(12), dp(12))
            topBar.setBackgroundResource(R.drawable.bg_overlay_panel)
            topBar.layoutParams = FrameLayout.LayoutParams(
                dp(150),
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP or Gravity.START,
            ).apply { setMargins(dp(12), dp(12), 0, 0) }

            modelText.layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(10) }

            fpsText.gravity = Gravity.START
            fpsText.layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(2) }

            bottomBar.setBackgroundColor(Color.TRANSPARENT)
            bottomBar.setPadding(0, 0, 0, 0)
            bottomBar.gravity = Gravity.CENTER
            bottomBar.layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.END or Gravity.CENTER_VERTICAL,
            ).apply { setMargins(0, 0, dp(12), 0) }

            buttonPanel.orientation = LinearLayout.VERTICAL
            buttonPanel.setBackgroundResource(R.drawable.bg_overlay_panel)
            buttonPanel.setPadding(dp(10), dp(12), dp(10), dp(12))

            rotateButton.layoutParams = railButtonParams(marginBottom = true)
            mirrorButton.layoutParams = railButtonParams(marginBottom = true)
            lampButton.layoutParams = railButtonParams(marginBottom = false)
        } else {
            videoArea.layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            ).apply {
                topMargin = dp(48)
                bottomMargin = dp(72)
            }

            topBar.orientation = LinearLayout.HORIZONTAL
            topBar.gravity = Gravity.CENTER_VERTICAL
            topBar.setPadding(dp(16), 0, dp(16), 0)
            topBar.setBackgroundColor(Color.BLACK)
            topBar.layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                dp(48),
                Gravity.TOP,
            )

            modelText.layoutParams = LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f
            ).apply { marginStart = dp(16) }

            fpsText.gravity = Gravity.END or Gravity.CENTER_VERTICAL
            fpsText.layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            )

            bottomBar.setBackgroundColor(Color.BLACK)
            bottomBar.setPadding(0, 0, 0, 0)
            bottomBar.gravity = Gravity.CENTER
            bottomBar.layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                dp(72),
                Gravity.BOTTOM,
            )

            buttonPanel.orientation = LinearLayout.HORIZONTAL
            buttonPanel.background = null
            buttonPanel.setPadding(0, 0, 0, 0)

            rotateButton.layoutParams = barButtonParams(marginEnd = true)
            mirrorButton.layoutParams = barButtonParams(marginEnd = true)
            lampButton.layoutParams = barButtonParams(marginEnd = false)
        }
    }

    private fun barButtonParams(marginEnd: Boolean): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(dp(48), dp(48)).apply {
            if (marginEnd) this.marginEnd = dp(16)
        }

    private fun railButtonParams(marginBottom: Boolean): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(dp(48), dp(48)).apply {
            if (marginBottom) bottomMargin = dp(16)
        }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).roundToInt()

    // ------------------------------------------------------------------
    // actions
    // ------------------------------------------------------------------

    /** Opens Android's Wi-Fi panel; the user selects the earpick network. */
    private fun openWifiPanel() {
        wifiPanelOpened = true
        try {
            startActivity(Intent(Settings.Panel.ACTION_INTERNET_CONNECTIVITY))
        } catch (_: Exception) {
            try {
                startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
            } catch (_: Exception) {
            }
        }
    }

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
