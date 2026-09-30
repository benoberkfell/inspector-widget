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
import java.util.concurrent.atomic.AtomicInteger

/**
 * Runs work on the app's main thread and returns its result synchronously.
 */
object MainThread {

    private const val TAG = "ViewSpector"

    /** Default wait for main-thread work when the current command set no other. */
    const val DEFAULT_TIMEOUT_MS = 5_000L

    /**
     * Waits for whole-tree work (an a11y or Compose walk, a property batch). Kept well
     * under the host's deadline for those commands (4 x 30 s by default), so the host
     * gets an ERROR reply instead of timing out and dropping the connection.
     */
    const val HEAVY_TIMEOUT_MS = 30_000L

    /** Waits for a View tree walk (DUMP_TREE; the host allows 30 s by default). */
    const val TREE_TIMEOUT_MS = 15_000L

    // A single Handler bound to the main Looper. Cheap to hold; posting is the
    // only operation we use. (ui-inspector ThreadUtils.kt:25.)
    private val mainHandler = Handler(Looper.getMainLooper())

    // The wait the current connection thread's command allows (Dispatcher sets it per
    // request); run() without an explicit timeout uses it.
    private val commandTimeoutMs = ThreadLocal<Long>()

    /** Sets the default [run] timeout for the rest of the calling thread's current command. */
    fun setCommandTimeout(timeoutMs: Long) {
        commandTimeoutMs.set(timeoutMs)
    }

    private fun defaultTimeoutMs(): Long = commandTimeoutMs.get() ?: DEFAULT_TIMEOUT_MS

    /**
     * Main-thread work that did not finish within its timeout. [started] = the work began
     * and may still be running on the main thread (it cannot be interrupted, so it still
     * owns whatever it writes to); false = it never started and was cancelled, so nothing
     * it would have touched is in use, unless [queued]: it never started but was left
     * queued (run with cancelOnTimeout = false) and runs once the main thread frees up.
     */
    class MainThreadTimeoutException(
        message: String,
        val started: Boolean,
        val queued: Boolean = false,
    ) : TimeoutException(message)

    // Task states: posted and waiting, running (or done), cancelled before it ran.
    private const val PENDING = 0
    private const val RUNNING = 1
    private const val CANCELLED = 2

    /**
     * Executes [block] on the main thread and returns its result.
     *
     * - If the caller is already on the main thread, [block] runs inline (no
     *   post, no deadlock — posting and then blocking on our own thread would
     *   hang forever). Mirrors ThreadUtils.runOnMainThread's inline fast path.
     * - Otherwise [block] is posted to the main [Looper] and the calling thread
     *   blocks on a [CountDownLatch] until it completes or [timeoutMs] elapses.
     *   On a timeout the post is removed from the queue and marked cancelled, so
     *   work that has not started never runs later against a request that already
     *   failed (and a wedged main thread does not pile up stale walks).
     * - Cleanup that must happen even if late (restoring what the payload changed in
     *   the app) passes [cancelOnTimeout] = false: the post then stays queued and runs
     *   once the main thread frees up, and the timeout only stops the wait.
     *
     * Any [Throwable] thrown by [block] is captured and re-thrown on the calling
     * thread so the [Dispatcher] can turn it into an ERROR response. A timeout
     * throws [MainThreadTimeoutException].
     *
     * @param timeoutMs max time to wait for the posted work; defaults to the current
     *   command's timeout ([setCommandTimeout]), else [DEFAULT_TIMEOUT_MS].
     * @param cancelOnTimeout false to leave work that has not started queued on a
     *   timeout (it runs later; the exception then says so), for cleanup.
     * @param block the work to run on the main thread; its return value is
     *   propagated back to the caller.
     */
    @Throws(TimeoutException::class)
    fun <T> run(
        timeoutMs: Long = defaultTimeoutMs(),
        cancelOnTimeout: Boolean = true,
        block: () -> T,
    ): T {
        // Fast path: already on the main thread -> run inline. Posting here and
        // awaiting would deadlock because the latch would never be counted down
        // until this same thread returned to the Looper.
        if (Looper.myLooper() == Looper.getMainLooper()) {
            return block()
        }

        val latch = CountDownLatch(1)
        val state = AtomicInteger(PENDING)
        // Hold result / failure across the thread boundary. Exactly one is set
        // before the latch counts down.
        var result: T? = null
        var failure: Throwable? = null

        val task = Runnable {
            // Claim the task; lose to a timeout that already cancelled it.
            if (!state.compareAndSet(PENDING, RUNNING)) return@Runnable
            try {
                result = block()
            } catch (t: Throwable) {
                failure = t
            } finally {
                latch.countDown()
            }
        }
        if (!mainHandler.post(task)) {
            throw IllegalStateException("the main Looper is exiting; cannot run main-thread work")
        }

        val completed =
            try {
                latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            } catch (ie: InterruptedException) {
                // Preserve the interrupt status and surface it to the caller.
                if (cancelOnTimeout && state.compareAndSet(PENDING, CANCELLED)) {
                    mainHandler.removeCallbacks(task)
                }
                Thread.currentThread().interrupt()
                Log.w(TAG, "MainThread.run interrupted while awaiting main-thread work", ie)
                throw ie
            }

        if (!completed) {
            if (!cancelOnTimeout && state.get() == PENDING) {
                // Left queued on purpose: it runs when the main thread frees up.
                Log.w(TAG, "MainThread.run: the main thread was busy for ${timeoutMs}ms; the work stays queued")
                throw MainThreadTimeoutException(
                    "the app's main thread was busy for ${timeoutMs}ms; the work stays queued " +
                        "and runs once it frees up",
                    started = false,
                    queued = true,
                )
            }
            if (cancelOnTimeout && state.compareAndSet(PENDING, CANCELLED)) {
                // Never started: drop it from the queue so it can't run later.
                mainHandler.removeCallbacks(task)
                Log.w(TAG, "MainThread.run: the main thread was busy for ${timeoutMs}ms; work cancelled")
                throw MainThreadTimeoutException(
                    "the app's main thread was busy for ${timeoutMs}ms, so the work never " +
                        "started (it was cancelled)",
                    started = false,
                )
            }
            // It started: it may have finished just now, else it is still running.
            if (latch.count > 0L) {
                Log.w(TAG, "MainThread.run timed out after ${timeoutMs}ms; the work is still running")
                throw MainThreadTimeoutException(
                    "main-thread work did not complete within ${timeoutMs}ms (it is still " +
                        "running on the app's main thread)",
                    started = true,
                )
            }
        }

        failure?.let { throw it }

        // `block` returned normally. A genuine null result is only valid when T
        // is nullable; the unchecked cast reproduces the caller's declared type.
        @Suppress("UNCHECKED_CAST")
        return result as T
    }
}
