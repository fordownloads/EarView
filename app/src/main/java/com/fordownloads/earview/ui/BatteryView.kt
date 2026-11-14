package com.fordownloads.earview.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.util.AttributeSet
import android.view.View
import com.fordownloads.earview.R

/**
 * Battery shell with a live fill level, percentage and charging dot.
 * Replaces the static battery icon so the remaining charge is visible.
 */
class BatteryView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private var percent = -1
    private var charging = false

    private val density = resources.displayMetrics.density
    private val scaledDensity = resources.displayMetrics.scaledDensity

    private val shellPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.4f)
    }
    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD)
        textSize = 13f * scaledDensity
    }

    private val okColor = context.getColor(R.color.battery_ok)
    private val lowColor = context.getColor(R.color.battery_low)
    private val criticalColor = context.getColor(R.color.battery_critical)
    private val unknownColor = context.getColor(R.color.battery_unknown)

    private val body = RectF()
    private val cap = RectF()
    private val fill = RectF()

    fun setValue(value: Int, isCharging: Boolean = false) {
        percent = value
        charging = isCharging
        invalidate()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        setMeasuredDimension(
            resolveSize(dp(80f).toInt(), widthMeasureSpec),
            resolveSize(dp(28f).toInt(), heightMeasureSpec),
        )
    }

    override fun onDraw(canvas: Canvas) {
        val iconTop = (height - dp(24f)) / 2f
        body.set(dp(1f), iconTop + dp(6f), dp(19f), iconTop + dp(18f))
        cap.set(dp(20.2f), iconTop + dp(9.5f), dp(22.2f), iconTop + dp(14.5f))

        val known = percent >= 0
        val levelColor = when {
            percent >= 50 -> okColor
            percent >= 20 -> lowColor
            percent >= 0 -> criticalColor
            else -> unknownColor
        }

        shellPaint.color = if (known) levelColor else unknownColor
        canvas.drawRoundRect(body, dp(2.2f), dp(2.2f), shellPaint)
        canvas.drawRoundRect(cap, dp(0.8f), dp(0.8f), shellPaint)

        if (known) {
            val left = body.left + dp(2.3f)
            val right = body.right - dp(2.3f)
            val top = body.top + dp(2.3f)
            val bottom = body.bottom - dp(2.3f)
            val fillWidth = (right - left) * percent.coerceIn(0, 100) / 100f
            if (fillWidth > 0f) {
                fill.set(left, top, left + fillWidth, bottom)
                fillPaint.color = levelColor
                canvas.drawRoundRect(fill, dp(1f), dp(1f), fillPaint)
            }
        }

        if (charging) {
            dotPaint.color = okColor
            canvas.drawCircle(dp(18.6f), iconTop + dp(5.2f), dp(2.4f), dotPaint)
        }

        textPaint.color = if (known) levelColor else Color.argb(140, 255, 255, 255)
        val text = if (known) "$percent%" else "--"
        val baseline = height / 2f - (textPaint.ascent() + textPaint.descent()) / 2f
        canvas.drawText(text, dp(27f), baseline, textPaint)
    }

    private fun dp(value: Float): Float = value * density
}
