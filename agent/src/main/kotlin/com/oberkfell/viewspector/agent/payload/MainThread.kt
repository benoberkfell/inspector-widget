/*
 * ViewSpector — payload core module.
 *
 * Synchronous "run this on the app main (UI) Looper and give me the result"
 * helper. The socket server thread must never touch live View state directly;
 * all tree/property/roots work hops onto the main thread via [MainThread.run]
 * and blocks for the result.
 *
 * Models on the real inspector's main-thread hop:
 *   - ThreadUtils.runOnMainThread (dynamic-layout-inspector util/ThreadUtils.kt):
 *     run inline if already on the main thread, else post + await.
 *   - MainThreadExecutor (ui-inspector .../inspectors/view/ThreadUtils.kt:24-30):
 *     Handler(Looper.getMainLooper()).post(...).
 * We add a CountDownLatch + result/exception capture so callers get the value
 * (or the original failure) back synchronously, with a timeout guard so a
 * blocked/janky UI thread can never wedge the socket connection forever.
 */
package com.oberkfell.viewspector.agent.payload

import android.os.Handler
import android.os.Looper
import android.util.Log
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/**
 * Runs work on the app's main thread and returns its result synchronously.
 */
object MainThread {

    private const val TAG = "ViewSpector"

    // A single Handler bound to the main Looper. Cheap to hold; posting is the
    // only operation we use. (ui-inspector ThreadUtils.kt:25.)
    private val mainHandler = Handler(Looper.getMainLooper())

    /**
     * Executes [block] on the main thread and returns its result.
     *
     * - If the caller is already on the main thread, [block] runs inline (no
     *   post, no deadlock — posting and then blocking on our own thread would
     *   hang forever). Mirrors ThreadUtils.runOnMainThread's inline fast path.
     * - Otherwise [block] is posted to the main [Looper] and the calling thread
     *   blocks on a [CountDownLatch] until it completes or [timeoutMs] elapses.
     *
     * Any [Throwable] thrown by [block] is captured and re-thrown on the calling
     * thread so the [Dispatcher] can turn it into an ERROR response. A timeout
     * throws [TimeoutException].
     *
     * @param timeoutMs max time to wait for the posted work (default 5000 ms).
     * @param block the work to run on the main thread; its return value is
     *   propagated back to the caller.
     */
    @Throws(TimeoutException::class)
    fun <T> run(timeoutMs: Long = 5000, block: () -> T): T {
        // Fast path: already on the main thread -> run inline. Posting here and
        // awaiting would deadlock because the latch would never be counted down
        // until this same thread returned to the Looper.
        if (Looper.myLooper() == Looper.getMainLooper()) {
            return block()
        }

        val latch = CountDownLatch(1)
        // Hold result / failure across the thread boundary. Exactly one is set
        // before the latch counts down.
        var result: T? = null
        var failure: Throwable? = null

        mainHandler.post {
            try {
                result = block()
            } catch (t: Throwable) {
                failure = t
            } finally {
                latch.countDown()
            }
        }

        val completed =
            try {
                latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            } catch (ie: InterruptedException) {
                // Preserve the interrupt status and surface it to the caller.
                Thread.currentThread().interrupt()
                Log.w(TAG, "MainThread.run interrupted while awaiting main-thread work", ie)
                throw ie
            }

        if (!completed) {
            // The main thread did not finish in time. The posted Runnable may
            // still run later and harmlessly set result/failure that nobody
            // reads. We surface a timeout so the connection stays responsive.
            Log.w(TAG, "MainThread.run timed out after ${timeoutMs}ms")
            throw TimeoutException("Main-thread work did not complete within ${timeoutMs}ms")
        }

        failure?.let { throw it }

        // `block` returned normally. A genuine null result is only valid when T
        // is nullable; the unchecked cast reproduces the caller's declared type.
        @Suppress("UNCHECKED_CAST")
        return result as T
    }
}
