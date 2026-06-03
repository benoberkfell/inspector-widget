/*
 * ViewSpector — PAYLOAD BITMAP CAPTURE
 *
 * Captures a root view's window surface to a Bitmap via android.view.PixelCopy and
 * encodes it onto the wire EXACTLY like Android Studio's real Layout Inspector:
 *
 *   9-byte header:
 *     [width  LE int32 @ 0]
 *     [height LE int32 @ 4]
 *     [BitmapType byte  @ 8]   (1=RGB_565, 2=ABGR_8888, 3=ARGB_8888)
 *   followed by raw Bitmap.copyPixelsToBuffer() pixels,
 *   the WHOLE buffer deflated with java.util.zip.Deflater(Deflater.BEST_SPEED).
 *
 * The host inflates with java.util.zip.Inflater and decodes the header + pixels.
 *
 * Modeled on the real inspector:
 *   - framework/BitmapExtensions.kt  (header layout, config->BitmapType mapping)
 *   - framework/SynchronousPixelCopy.java  (HandlerThread + latch/wait blocking copy)
 *   - framework/ViewExtensions.kt:65-88  (viewRootImpl.mSurface, getLocationInSurface,
 *                                         scaled dest bitmap, PixelCopy.SUCCESS check)
 *   - common/BitmapUtils.kt  (BITMAP_HEADER_SIZE = 9, BitmapType byte values)
 *   - common/LayoutInspectorUtils.kt:70  (Int.toBytes little-endian)
 *   - util/ZipUtils.kt:24  (ByteArray.compress() == Deflater(BEST_SPEED))
 *
 * SKP is explicitly OUT of scope; BITMAP only.
 */
package com.oberkfell.viewspector.agent.payload

import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.PixelCopy
import android.view.Surface
import android.view.View
import android.view.ViewDebug
import com.oberkfell.viewspector.proto.ViewInspection
import com.google.protobuf.ByteString
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.Callable
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit
import java.util.zip.Deflater
import kotlin.math.roundToInt

/**
 * Captures the window backing [root] to a [ViewInspection.Screenshot].
 *
 * All reflection and framework calls are guarded; any failure is logged under the
 * "ViewSpector" tag and yields an empty Screenshot (width == height == 0) rather than
 * propagating, so a screenshot failure can never take down a tree dump.
 */
object Capture {

    private const val TAG = "ViewSpector"

    /** Header size: width(4) + height(4) + type(1). Mirrors common BitmapUtils.BITMAP_HEADER_SIZE. */
    private const val BITMAP_HEADER_SIZE = 9

    /**
     * Wire BitmapType byte values (see proto Screenshot.bitmap_type and common/BitmapUtils.kt):
     *   1 = RGB_565, 2 = ABGR_8888, 3 = ARGB_8888.
     * A [Bitmap.Config.ARGB_8888] bitmap is emitted on the wire as ABGR_8888 (byte 2),
     * matching BitmapExtensions.kt:38-43.
     */
    private const val WIRE_RGB_565: Byte = 1
    private const val WIRE_ABGR_8888: Byte = 2

    /** Max time to wait for a single PixelCopy to complete (matches SynchronousPixelCopy ~1s). */
    private const val PIXEL_COPY_TIMEOUT_MS = 1000L

    /**
     * Capture [root]'s window to a Screenshot scaled by [scale] (clamped to (0, 1]).
     *
     * @return a populated BITMAP Screenshot, or an empty (width==height==0) one on any failure.
     */
    @JvmStatic
    fun screenshot(root: View, scale: Float): ViewInspection.Screenshot {
        val empty = emptyScreenshot(scale)
        return try {
            val effectiveScale = if (scale.isNaN() || scale <= 0f) 1f else minOf(scale, 1f)

            val srcWidth = root.width
            val srcHeight = root.height
            if (srcWidth <= 0 || srcHeight <= 0) {
                Log.w(TAG, "Capture: root has non-positive size ${srcWidth}x$srcHeight")
                return empty
            }

            val scaledWidth = (srcWidth * effectiveScale).roundToInt().coerceAtLeast(1)
            val scaledHeight = (srcHeight * effectiveScale).roundToInt().coerceAtLeast(1)

            // Resolve the window's Surface via View.getViewRootImpl().mSurface (ViewExtensions.kt:68).
            // ViewRootImpl / mSurface are hidden, so this is reflective and guarded.
            val surface = resolveSurface(root)
            if (surface == null || !surface.isValid) {
                Log.w(TAG, "Capture: no valid surface for root")
                return empty
            }

            // ARGB_8888 destination; emitted as ABGR_8888 on the wire (BitmapExtensions.kt:38-43).
            val bitmap = try {
                Bitmap.createBitmap(scaledWidth, scaledHeight, Bitmap.Config.ARGB_8888)
            } catch (t: Throwable) {
                Log.w(TAG, "Capture: Bitmap.createBitmap failed", t)
                return empty
            }

            // Source rect = this root's position within the surface (ViewExtensions.kt:74-76).
            // PixelCopy scales the source rect into the (smaller) destination bitmap.
            val location = IntArray(2)
            val srcRect: Rect = try {
                root.getLocationInSurface(location)
                Rect(location[0], location[1], location[0] + srcWidth, location[1] + srcHeight)
            } catch (t: Throwable) {
                // getLocationInSurface is hidden on some images; fall back to the whole surface.
                Log.w(TAG, "Capture: getLocationInSurface failed, using full-surface rect", t)
                Rect(0, 0, srcWidth, srcHeight)
            }

            val result = requestPixelCopy(surface, srcRect, bitmap)
            if (result != PixelCopy.SUCCESS) {
                Log.w(TAG, "Capture: PixelCopy returned error code $result")
                bitmap.recycle()
                return empty
            }

            val wireType = configToWireType(bitmap.config)
            if (wireType == null) {
                Log.w(TAG, "Capture: unsupported bitmap config ${bitmap.config}")
                bitmap.recycle()
                return empty
            }

            val raw = bitmapToByteArray(bitmap, wireType)
            val compressed = compress(raw)
            bitmap.recycle()

            ViewInspection.Screenshot.newBuilder()
                .setFormat(ViewInspection.Screenshot.Format.BITMAP)
                .setWidth(scaledWidth)
                .setHeight(scaledHeight)
                .setBitmapType(wireType.toInt())
                .setData(ByteString.copyFrom(compressed))
                .setScale(effectiveScale)
                .build()
        } catch (t: Throwable) {
            Log.w(TAG, "Capture: unexpected failure", t)
            empty
        }
    }

    // ------------------------------------------------------------------ surface

    /**
     * Resolve the Surface backing [view]'s window.
     *
     * Strategy (all reflective, all guarded):
     *   1. view.getViewRootImpl()  -> ViewRootImpl.mSurface   (matches ViewExtensions.kt:68)
     *   2. fallback: view.getViewRootImpl() -> getSurface()    (where available)
     */
    private fun resolveSurface(view: View): Surface? {
        // (1) ViewRootImpl.mSurface — the path the real inspector uses.
        try {
            val getViewRootImpl = View::class.java.getMethod("getViewRootImpl")
            val viewRootImpl = getViewRootImpl.invoke(view) ?: return null
            val mSurfaceField = viewRootImpl.javaClass.getDeclaredField("mSurface")
            mSurfaceField.isAccessible = true
            (mSurfaceField.get(viewRootImpl) as? Surface)?.let { return it }
        } catch (t: Throwable) {
            Log.w(TAG, "Capture: ViewRootImpl.mSurface reflection failed", t)
        }

        // (2) ViewRootImpl.getSurface() — alternate accessor on some builds.
        try {
            val getViewRootImpl = View::class.java.getMethod("getViewRootImpl")
            val viewRootImpl = getViewRootImpl.invoke(view) ?: return null
            val getSurface = viewRootImpl.javaClass.getMethod("getSurface")
            (getSurface.invoke(viewRootImpl) as? Surface)?.let { return it }
        } catch (t: Throwable) {
            Log.w(TAG, "Capture: ViewRootImpl.getSurface() reflection failed", t)
        }

        return null
    }

    // ----------------------------------------------------------------- pixelcopy

    /**
     * Synchronously copy [srcRect] of [surface] into [dest], scaling to dest's size.
     *
     * The request is posted to a dedicated HandlerThread (per the PixelCopy contract — the
     * listener fires on that Handler's thread, never the caller's), and the caller blocks on a
     * CountDownLatch with a ~1s timeout. Modeled on SynchronousPixelCopy.java.
     *
     * @return a PixelCopy.* result code, or PixelCopy.ERROR_TIMEOUT on timeout / dispatch failure.
     */
    private fun requestPixelCopy(surface: Surface, srcRect: Rect, dest: Bitmap): Int {
        val thread = HandlerThread("ViewSpector-PixelCopy")
        thread.start()
        return try {
            val handler = Handler(thread.looper)
            val latch = CountDownLatch(1)
            val status = intArrayOf(PixelCopy.ERROR_TIMEOUT)
            try {
                PixelCopy.request(
                    surface,
                    srcRect,
                    dest,
                    { copyResult ->
                        status[0] = copyResult
                        latch.countDown()
                    },
                    handler,
                )
            } catch (t: Throwable) {
                Log.w(TAG, "Capture: PixelCopy.request threw", t)
                return PixelCopy.ERROR_UNKNOWN
            }
            if (!latch.await(PIXEL_COPY_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
                Log.w(TAG, "Capture: PixelCopy timed out after ${PIXEL_COPY_TIMEOUT_MS}ms")
                return PixelCopy.ERROR_TIMEOUT
            }
            status[0]
        } finally {
            thread.quitSafely()
        }
    }

    // -------------------------------------------------------------------- encode

    /**
     * Encode [bitmap] into the wire byte array: 9-byte header + raw pixels.
     * Mirrors BitmapExtensions.kt:25-36 (Bitmap.toByteArray).
     */
    private fun bitmapToByteArray(bitmap: Bitmap, wireType: Byte): ByteArray {
        val pixelBytes = bitmap.byteCount
        val bytes = ByteArray(pixelBytes + BITMAP_HEADER_SIZE)

        toBytes(bitmap.width, bytes, 0)
        toBytes(bitmap.height, bytes, 4)
        bytes[8] = wireType

        val buf = ByteBuffer.wrap(bytes, BITMAP_HEADER_SIZE, pixelBytes)
        bitmap.copyPixelsToBuffer(buf)
        return bytes
    }

    /**
     * Write [value] into [bytes] at [offset] as little-endian int32.
     * Mirrors LayoutInspectorUtils.kt:70 (Int.toBytes).
     */
    private fun toBytes(value: Int, bytes: ByteArray, offset: Int) {
        for (i in 0..3) {
            bytes[offset + i] = (value ushr (i * 8) and 0xFF).toByte()
        }
    }

    /**
     * Deflate [input] with Deflater.BEST_SPEED.
     * Mirrors ZipUtils.kt:24 (ByteArray.compress).
     */
    private fun compress(input: ByteArray): ByteArray {
        val deflater = Deflater(Deflater.BEST_SPEED)
        deflater.setInput(input)
        deflater.finish()

        val baos = ByteArrayOutputStream()
        val buffer = ByteArray(4096)
        try {
            while (!deflater.finished()) {
                val count = deflater.deflate(buffer)
                if (count <= 0) break
                baos.write(buffer, 0, count)
            }
        } finally {
            deflater.end()
        }
        return baos.toByteArray()
    }

    /**
     * Map a [Bitmap.Config] to its wire BitmapType byte.
     * ARGB_8888 -> ABGR_8888 (2), RGB_565 -> RGB_565 (1). See BitmapExtensions.kt:38-43.
     * Returns null for configs we cannot represent on the wire.
     */
    private fun configToWireType(config: Bitmap.Config?): Byte? =
        when (config) {
            Bitmap.Config.ARGB_8888 -> WIRE_ABGR_8888
            Bitmap.Config.RGB_565 -> WIRE_RGB_565
            else -> null
        }

    /** An empty BITMAP screenshot (width == height == 0) used as the failure sentinel. */
    private fun emptyScreenshot(scale: Float): ViewInspection.Screenshot {
        val s = if (scale.isNaN() || scale <= 0f) 1f else minOf(scale, 1f)
        return ViewInspection.Screenshot.newBuilder()
            .setFormat(ViewInspection.Screenshot.Format.BITMAP)
            .setWidth(0)
            .setHeight(0)
            .setBitmapType(0)
            .setData(ByteString.EMPTY)
            .setScale(s)
            .build()
    }

    /** Result of an SKP capture. */
    class SkpResult(val supported: Boolean, val skp: ByteArray?, val error: String?)

    /**
     * Capture one frame of [root]'s rendering as a serialized Skia picture (SKP), via the public-ish
     * ViewDebug.startRenderingCommandsCapture(View, Executor, Callable<OutputStream>) (API 33+/T).
     * The system invokes our Executor once per drawn frame with a runnable that serializes the
     * picture into the OutputStream from our Callable; we invalidate once, await the first frame,
     * then close. Returns the raw SKP bytes ("skiapict" magic + LE version + body).
     */
    fun captureSkp(root: View, timeoutMs: Long = 4000): SkpResult {
        if (Build.VERSION.SDK_INT <= 32) {
            return SkpResult(false, null, "SKP capture needs API 33+ (have ${Build.VERSION.SDK_INT})")
        }
        val os = ByteArrayOutputStream()
        val latch = CountDownLatch(1)
        // The system calls our Executor (on the render thread) with a runnable that serializes the
        // picture. Mirror the real CaptureExecutor: re-post that runnable to a dedicated worker
        // thread and only signal completion once it has finished writing the stream.
        val worker = java.util.concurrent.Executors.newSingleThreadExecutor()
        val executor = Executor { command ->
            worker.execute { try { command.run() } finally { latch.countDown() } }
        }
        val method = ViewDebug::class.java.getDeclaredMethod(
            "startRenderingCommandsCapture",
            View::class.java, Executor::class.java, Callable::class.java,
        )
        // startRenderingCommandsCapture() must run on the View's UI thread; register + invalidate
        // there, then await the captured frame OFF the main thread so rendering can proceed.
        val handle: AutoCloseable = try {
            MainThread.run {
                val h = method.invoke(null, root, executor, Callable { os }) as AutoCloseable
                root.invalidate()
                h
            }
        } catch (t: Throwable) {
            val cause = (t as? java.lang.reflect.InvocationTargetException)?.targetException ?: t.cause ?: t
            Log.w("ViewSpector", "startRenderingCommandsCapture failed", cause)
            return SkpResult(false, null, "startRenderingCommandsCapture: ${cause.javaClass.name}: ${cause.message}")
        }
        try {
            if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                return SkpResult(true, null, "timed out waiting for an SKP frame")
            }
        } catch (t: Throwable) {
            return SkpResult(true, null, "capture error: ${t.message}")
        } finally {
            try { MainThread.run { handle.close() } } catch (_: Throwable) {}
            worker.shutdownNow()
        }
        val bytes = os.toByteArray()
        return if (bytes.isEmpty()) SkpResult(true, null, "empty SKP") else SkpResult(true, bytes, null)
    }
}
