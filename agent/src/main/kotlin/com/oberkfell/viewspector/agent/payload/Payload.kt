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
 *
 * Build handshake: start() hashes the payload.jar it was loaded from (SHA-256,
 * the same value scripts/build.sh writes to build-out/BUILD_ID) into
 * [buildId], which HELLO reports as "viewspector-<v>+<buildId>". The host
 * compares it with its own payload.jar and replaces a stale agent.
 */
package com.oberkfell.viewspector.agent.payload

import android.util.Log
import java.io.File
import java.security.MessageDigest
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
     * SHA-256 (lowercase hex) of the payload.jar this code was loaded from, or
     * "unknown" if it could not be read. Set once by [start].
     */
    @Volatile
    @JvmStatic
    var buildId: String = UNKNOWN_BUILD
        private set

    private const val UNKNOWN_BUILD = "unknown"

    /**
     * Starts the ViewSpector socket server bound to [socketName] on a fresh
     * background thread. Returns immediately. Calling more than once is safe and
     * does nothing after the first successful start.
     *
     * @param socketName the abstract LocalServerSocket name, e.g.
     *   `viewspector_<pid>` (CONTRACT.md §3).
     */
    @JvmStatic
    fun start(socketName: String) = start(socketName, null)

    /**
     * As [start], with the path of the payload.jar the Bootstrap loaded, so
     * [buildId] hashes exactly that file. (A Bootstrap from an older injection
     * that is still in the bootstrap classloader calls the one-argument form;
     * the jar is then found through this class's own classloader.)
     */
    @JvmStatic
    fun start(socketName: String, payloadPath: String?) {
        if (!started.compareAndSet(false, true)) {
            Log.i(TAG, "Payload.start ignored: server already started for @$socketName")
            return
        }

        buildId = computeBuildId(payloadPath)
        Log.i(TAG, "Payload.start: launching ViewSpector server on @$socketName (build $buildId)")

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

    private fun computeBuildId(payloadPath: String?): String {
        val jar = payloadPath?.let(::File)?.takeIf { it.isFile } ?: ownJar()
        if (jar == null) {
            Log.w(TAG, "Payload: could not locate payload.jar; build id unknown")
            return UNKNOWN_BUILD
        }
        return try {
            sha256Hex(jar)
        } catch (t: Throwable) {
            Log.w(TAG, "Payload: could not hash ${jar.path}; build id unknown", t)
            UNKNOWN_BUILD
        }
    }

    /**
     * The jar this class was loaded from: the "classes.dex" resource that this
     * classloader sees but its parent (the app classloader) does not.
     */
    private fun ownJar(): File? =
        try {
            val loader = Payload::class.java.classLoader
            val inherited =
                loader?.parent?.getResources("classes.dex")?.toList()?.map { it.toString() }?.toSet()
                    ?: emptySet()
            loader?.getResources("classes.dex")?.toList()
                ?.map { it.toString() }
                ?.firstOrNull { it.startsWith("jar:file:") && it !in inherited }
                ?.removePrefix("jar:file:")
                ?.substringBefore("!/")
                ?.let(::File)
                ?.takeIf { it.isFile }
        } catch (t: Throwable) {
            Log.w(TAG, "Payload: could not locate its own jar", t)
            null
        }

    private fun sha256Hex(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                digest.update(buffer, 0, n)
            }
        }
        return digest.digest().joinToString("") { b -> "%02x".format(b.toInt() and 0xff) }
    }
}
