/*
 * ViewSpector — payload core module.
 *
 * Payload entrypoint. The Bootstrap layer (com.oberkfell.viewspector.agent.
 * Bootstrap), having loaded this jar in a DexClassLoader whose parent is the
 * app classloader, calls Payload.start(socketName) once. We spin up the socket
 * [Server] on its own thread and return immediately so we never block the
 * injection / app thread (CONTRACT.md §6: never block the main thread).
 *
 * Idempotent: a second start() with a server already running is a no-op, so a
 * double injection (or a re-attach) does not bind a second socket or leak a
 * thread. Models on the UI-Inspector launcher's single-server lifecycle
 * (ui-inspector Server.kt:43 startServer + InspectorLauncher), simplified to a
 * plain thread.
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The static entrypoint Bootstrap invokes via reflection / direct call.
 */
object Payload {

    private const val TAG = "ViewSpector"

    // Guards against double-start. Set true the first time start() wins the
    // race; never reset for the life of the process (a stopped server is not
    // expected to be restarted within the same injection).
    private val started = AtomicBoolean(false)

    // The running server thread, kept so the (rare) caller that wants to await
    // teardown can reach it; primarily here to keep a strong reference so the
    // thread is not mistaken for unreferenced.
    @Volatile
    private var serverThread: Thread? = null

    @Volatile
    private var server: Server? = null

    /**
     * Starts the ViewSpector socket server bound to [socketName] on a fresh
     * background thread. Returns immediately. Calling more than once is safe and
     * does nothing after the first successful start.
     *
     * @param socketName the abstract LocalServerSocket name, e.g.
     *   `viewspector_<pid>` (CONTRACT.md §3).
     */
    @JvmStatic
    fun start(socketName: String) {
        if (!started.compareAndSet(false, true)) {
            Log.i(TAG, "Payload.start ignored: server already started for @$socketName")
            return
        }

        Log.i(TAG, "Payload.start: launching ViewSpector server on @$socketName")

        val srv = Server(socketName)
        server = srv

        val thread =
            Thread({
                try {
                    srv.run()
                } catch (t: Throwable) {
                    // run() handles its own errors, but never let an exception
                    // escape the thread silently.
                    Log.e(TAG, "ViewSpector server thread crashed", t)
                } finally {
                    Log.i(TAG, "ViewSpector server thread exiting for @$socketName")
                }
            }, "viewspector-server")
        // Daemon so the server thread never keeps the app process alive on its
        // own; the app's lifecycle governs ours.
        thread.isDaemon = true
        serverThread = thread
        thread.start()

        Log.i(TAG, "Payload.start: server thread started for @$socketName")
    }
}
