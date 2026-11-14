package com.fordownloads.earview.ui

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Camera
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import android.view.animation.DecelerateInterpolator
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Aspect-fit JPEG preview with rotation and horizontal mirror.
 *
 * Gestures:
 *  - horizontal one-finger swipe drives a perspective flip and toggles mirror
 *  - two-finger rotate previews live and snaps to the nearest 90 degrees
 */
class VideoView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    interface Listener {
        fun onMirrorToggle()

        /** Snapped visual rotation (includes any mount offset). */
        fun onRotationCommit(baseDegrees: Int)
    }

    var listener: Listener? = null

    private var bitmap: Bitmap? = null

    /** Currently displayed rotation in degrees. */
    private var rotation = 0f
    private var rotationAnimator: ValueAnimator? = null

    private var mirrored = false

    /** Perspective Y rotation used for flip animations, 0..±180. */
    private var mirrorFlip = 0f
    private var mirrorAnimator: ValueAnimator? = null

    private val paint = Paint(Paint.FILTER_BITMAP_FLAG or Paint.DITHER_FLAG)
    private val camera = Camera()
    private val flipMatrix = Matrix()
    private val density = resources.displayMetrics.density
    private val swipeTravel = 160f * density

    private var downX = 0f
    private var downY = 0f
    private var downAt = 0L
    private var ignoreGesture = false
    private var multiTouch = false
    private var lastAngle = 0f
    private var gestureStartRotation = 0f
    private var gestureDelta = 0f

    init {
        setBackgroundColor(Color.BLACK)
        isClickable = true
        camera.setLocation(0f, 0f, -8f * density)
    }

    fun setBitmap(value: Bitmap) {
        bitmap = value
        invalidate()
    }

    fun setBaseRotation(degrees: Int, animate: Boolean = true) {
        val target = normalize(degrees.toFloat())
        if (!animate || bitmap == null) {
            rotationAnimator?.cancel()
            rotation = target
            invalidate()
            return
        }
        animateRotationTo(target)
    }

    fun setMirror(value: Boolean, animate: Boolean = true) {
        if (value == mirrored && mirrorFlip == 0f) return
        if (!animate || bitmap == null) {
            mirrorAnimator?.cancel()
            mirrored = value
            mirrorFlip = 0f
            invalidate()
            return
        }
        val target = if (mirrorFlip < 0f) -180f else 180f
        animateMirror(target, onEnd = {
            mirrored = value
            invalidate()
        })
    }

    fun hasFrame(): Boolean = bitmap != null

    override fun onDraw(canvas: Canvas) {
        val source = bitmap ?: return
        val bitmapWidth = source.width.toFloat()
        val bitmapHeight = source.height.toFloat()
        if (bitmapWidth <= 0f || bitmapHeight <= 0f) return

        val radians = Math.toRadians(rotation.toDouble())
        val cosValue = abs(cos(radians)).toFloat()
        val sinValue = abs(sin(radians)).toFloat()
        val rotatedWidth = bitmapWidth * cosValue + bitmapHeight * sinValue
        val rotatedHeight = bitmapWidth * sinValue + bitmapHeight * cosValue
        if (rotatedWidth <= 0f || rotatedHeight <= 0f) return

        val scale = min(width / rotatedWidth, height / rotatedHeight)

        canvas.save()

        // Perspective flip in screen space, around the view centre.
        if (mirrorFlip != 0f) {
            camera.save()
            camera.rotateY(mirrorFlip)
            camera.getMatrix(flipMatrix)
            camera.restore()
            flipMatrix.preTranslate(-width / 2f, -height / 2f)
            flipMatrix.postTranslate(width / 2f, height / 2f)
            canvas.concat(flipMatrix)
        }

        canvas.translate(width / 2f, height / 2f)
        if (mirrored) canvas.scale(-1f, 1f)
        canvas.rotate(rotation)
        canvas.scale(scale, scale)
        canvas.translate(-bitmapWidth / 2f, -bitmapHeight / 2f)
        canvas.drawBitmap(source, 0f, 0f, paint)
        canvas.restore()
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = event.x
                downY = event.y
                downAt = System.currentTimeMillis()
                multiTouch = false
                gestureDelta = 0f
                ignoreGesture = mirrorAnimator?.isRunning == true
                return true
            }

            MotionEvent.ACTION_POINTER_DOWN -> {
                if (event.pointerCount >= 2 && !ignoreGesture) {
                    multiTouch = true
                    lastAngle = angleBetween(event)
                    gestureStartRotation = rotation
                    gestureDelta = 0f
                    rotationAnimator?.cancel()
                    parent?.requestDisallowInterceptTouchEvent(true)
                }
                return true
            }

            MotionEvent.ACTION_MOVE -> {
                if (ignoreGesture) return true
                if (event.pointerCount >= 2) {
                    val angle = angleBetween(event)
                    var delta = angle - lastAngle
                    while (delta > 180f) delta -= 360f
                    while (delta < -180f) delta += 360f
                    // A mirrored image rotates the opposite way on screen.
                    gestureDelta += if (mirrored) -delta else delta
                    lastAngle = angle
                    rotation = normalize(gestureStartRotation + gestureDelta)
                    invalidate()
                } else if (!multiTouch) {
                    val dx = event.x - downX
                    mirrorFlip = (dx / swipeTravel).coerceIn(-1f, 1f) * 90f
                    invalidate()
                }
                return true
            }

            MotionEvent.ACTION_POINTER_UP -> {
                if (!ignoreGesture && multiTouch && event.pointerCount == 2) commitRotation()
                return true
            }

            MotionEvent.ACTION_UP -> {
                if (!ignoreGesture) {
                    if (multiTouch) {
                        if (abs(gestureDelta) > 0.5f) commitRotation()
                        multiTouch = false
                    } else {
                        val dx = event.x - downX
                        val dy = event.y - downY
                        val elapsed = System.currentTimeMillis() - downAt
                        if (abs(dx) > 80f * density &&
                            abs(dx) > abs(dy) * 2f &&
                            elapsed < 600
                        ) {
                            animateMirror(if (dx > 0f) 180f else -180f, toggle = true)
                        } else {
                            settleMirror()
                        }
                    }
                }
                ignoreGesture = false
                multiTouch = false
                gestureDelta = 0f
                return true
            }

            MotionEvent.ACTION_CANCEL -> {
                ignoreGesture = false
                multiTouch = false
                gestureDelta = 0f
                if (mirrorFlip != 0f) settleMirror()
                return true
            }
        }
        return super.onTouchEvent(event)
    }

    private fun commitRotation() {
        val raw = gestureStartRotation + gestureDelta
        var snapped = (raw / 90f).roundToInt() * 90
        snapped = ((snapped % 360) + 360) % 360
        rotation = normalize(raw)
        gestureDelta = 0f
        animateRotationTo(snapped.toFloat())
        listener?.onRotationCommit(snapped)
    }

    private fun animateRotationTo(target: Float) {
        rotationAnimator?.cancel()
        val delta = ((target - rotation + 540f) % 360f) - 180f
        if (abs(delta) < 0.01f) {
            rotation = normalize(target)
            invalidate()
            return
        }
        rotationAnimator = ValueAnimator.ofFloat(rotation, rotation + delta).apply {
            duration = 240
            interpolator = DecelerateInterpolator()
            addUpdateListener {
                rotation = normalize(it.animatedValue as Float)
                invalidate()
            }
            start()
        }
    }

    private fun settleMirror() {
        if (mirrorFlip == 0f) return
        animateMirror(0f)
    }

    /**
     * Animates the perspective flip. With [toggle] the mirror state changes when
     * the flip finishes, which is what a swipe does; otherwise [onEnd] runs.
     */
    private fun animateMirror(
        target: Float,
        toggle: Boolean = false,
        onEnd: (() -> Unit)? = null,
    ) {
        mirrorAnimator?.let { previous ->
            previous.removeAllListeners()
            previous.cancel()
        }
        mirrorAnimator = null
        val start = mirrorFlip
        val animator = ValueAnimator.ofFloat(start, target)
        animator.duration = (200f * (abs(target - start) / 180f)).toLong().coerceIn(90L, 280L)
        animator.interpolator = DecelerateInterpolator()
        animator.addUpdateListener {
            mirrorFlip = it.animatedValue as Float
            invalidate()
        }
        animator.addListener(object : AnimatorListenerAdapter() {
            override fun onAnimationEnd(animation: Animator) {
                if (toggle) {
                    mirrored = !mirrored
                    listener?.onMirrorToggle()
                }
                onEnd?.invoke()
                mirrorFlip = 0f
                invalidate()
            }
        })
        mirrorAnimator = animator
        animator.start()
    }

    private fun angleBetween(event: MotionEvent): Float {
        val dx = event.getX(1) - event.getX(0)
        val dy = event.getY(1) - event.getY(0)
        return Math.toDegrees(atan2(dy.toDouble(), dx.toDouble())).toFloat()
    }

    private fun normalize(degrees: Float): Float {
        var value = degrees % 360f
        if (value < 0f) value += 360f
        return value
    }
}
