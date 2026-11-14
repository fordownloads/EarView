package com.fordownloads.earview

import android.content.Context

/** Persisted video transform state (replaces the Python config.json). */
class Prefs(context: Context) {

    private val prefs = context.getSharedPreferences("earview", Context.MODE_PRIVATE)

    var rotation: Int
        get() {
            val value = prefs.getInt(KEY_ROTATION, 0)
            return if (value % 90 == 0) ((value % 360) + 360) % 360 else 0
        }
        set(value) {
            prefs.edit().putInt(KEY_ROTATION, ((value % 360) + 360) % 360).apply()
        }

    var mirror: Boolean
        get() = prefs.getBoolean(KEY_MIRROR, false)
        set(value) {
            prefs.edit().putBoolean(KEY_MIRROR, value).apply()
        }

    private companion object {
        const val KEY_ROTATION = "rotation"
        const val KEY_MIRROR = "mirror"
    }
}
