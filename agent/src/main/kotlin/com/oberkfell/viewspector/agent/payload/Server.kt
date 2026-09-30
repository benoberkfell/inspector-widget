/*
 * ViewSpector — payload core module.
 *
 * The on-device socket server. Listens on an abstract LocalServerSocket named
 * `viewspector_<pid>` (CONTRACT.md §3), accepts connections, and for each one
 * runs a synchronous request/response loop: read a framed Request, dispatch it,
 * write a framed Response. One request in flight per connection.
 *
 * Models on the real UI-Inspector server loop:
 *   android-sources/tools-base/ui-inspector/agent/payload/src/main/java/
 *     com/android/tools/ui/inspector/payload/Server.kt:43-131  (accept loop,
 *     5-minute inactivity timeout, shutdown signal, socket cleanup)
 *   .../payload/SessionHandler.kt:70-160  (per-connection read/handle/reply,
 *     EOF/IO handling, SHUTDOWN terminates the session and signals the server)
 * Rewritten to use plain JDK threads instead of kotlinx.coroutines so the
 * payload carries no coroutine runtime into the app classloader (CONTRACT.md §8:
 * no heavyweight deps).
 */
package com.oberkfell.viewspector.agent.payload

import android.net.LocalServerSocket
import android.net.LocalSocket
import android.net.LocalSocketAddress
import android.util.Log
import com.oberkfell.viewspector.proto.ViewInspection
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Owns the [LocalServerSocket] lifecycle for one payload session.
 *
 * @param socketName the abstract socket name to bind (e.g. `viewspector_12345`).
 */
class Server(private val socketName: String) {

    private companion object {
        const val TAG = "ViewSpector"

        // CONTRACT.md §2 / ui-inspector Server.kt:40 — close the server after 5
        // minutes with no client connected, so an abandoned injection does not
        // linger forever (a connected client is never idle; see the watchdog).
        val IDLE_TIMEOUT_MS = TimeUnit.MINUTES.toMillis(5)

        // Granularity of the idle watchdog's wakeups.
        val WATCHDOG_TICK_MS = TimeUnit.SECONDS.toMillis(5)
    }

    // True once the server has been asked to stop (shutdown command, idle
    // timeout, or fatal error). Read by the accept loop and the watchdog.
    private val stopped = AtomicBoolean(false)

    // The bound server socket. Held so stop() can close it to unblock accept().
    @Volatile
    private var serverSocket: LocalServerSocket? = null

    // Timestamp (uptime millis via System.nanoTime) of the last accepted
    // connection, completed request or disconnect, used by the idle watchdog
    // (which never fires while a client is connected).
    @Volatile
    private var lastActivityNanos: Long = System.nanoTime()

    // Latch tripped when the server has fully torn down; lets run() block its
    // caller until shutdown completes.
    private val terminated = CountDownLatch(1)

    // Serializes device work so concurrent client connections never interleave
    // (they queue rather than wedge) — one request is handled at a time. HELLO
    // and SHUTDOWN don't take it (see serveConnection).
    private val handleLock = Any()

    // Live client sockets, so stop() can shut them down to unblock their reads.
    private val activeClients =
        java.util.Collections.synchronizedSet(java.util.HashSet<LocalSocket>())

    /**
     * Binds the socket and runs the accept loop on the CALLING thread until a
     * shutdown is requested or the idle timeout fires. [Payload.start] invokes
     * this from a dedicated background thread, so it is fine to block here.
     */
    fun run() {
        // SHUTDOWN is carried out by serveConnection, after the reply is written.
        val dispatcher = Dispatcher()

        val socket =
            try {
                LocalServerSocket(socketName)
            } catch (e: IOException) {
                // "Address already in use" means an earlier injection's server
                // still owns the name. That is NOT benign: this (possibly newer)
                // payload will not serve, and the host would keep talking to the
                // old one. The host stops the old agent (SHUTDOWN) before
                // re-injecting and checks the build id in Hello, so this only
                // happens when the old agent could not be reached.
                if (e.message?.contains("already in use", ignoreCase = true) == true) {
                    Log.e(
                        TAG,
                        "Cannot bind @$socketName: another ViewSpector server (an earlier " +
                            "injection) still holds it, so this payload (build ${Payload.buildId}) " +
                            "will not serve. Stop the old agent (detach / SHUTDOWN) or restart " +
                            "the app, then inject again.",
                        e,
                    )
                } else {
                    Log.e(TAG, "Failed to bind LocalServerSocket @$socketName", e)
                }
                terminated.countDown()
                return
            }

        serverSocket = socket
        Log.i(TAG, "ViewSpector server listening on @$socketName")

        val watchdog = startIdleWatchdog()

        try {
            acceptLoop(socket, dispatcher)
        } finally {
            stopped.set(true)
            closeQuietly(socket)
            serverSocket = null
            watchdog.interrupt()
            Log.i(TAG, "ViewSpector server on @$socketName stopped")
            terminated.countDown()
        }
    }

    /**
     * Blocks until the server has fully terminated, or [timeoutMs] elapses.
     * Returns true if the server terminated within the timeout.
     */
    fun awaitTermination(timeoutMs: Long): Boolean =
        try {
            terminated.await(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (ie: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }

    /**
     * Requests an orderly shutdown: flags stopped, closes the server socket and
     * wakes the accept loop so it exits and the abstract name is released, and
     * disconnects every client. Idempotent.
     */
    fun stop() {
        if (stopped.compareAndSet(false, true)) {
            Log.i(TAG, "ViewSpector server shutdown requested")
            closeQuietly(serverSocket)
            // LocalServerSocket.close() does NOT wake a thread blocked in
            // accept(), and that blocked call keeps the socket (and its name in
            // /proc/net/unix) alive, so a new injection could not bind it.
            // Connect once: accept() returns, the loop sees `stopped`, exits.
            wakeAcceptLoop()
            // Disconnect every client. close() alone is not enough, for the same
            // reason: it doesn't wake a serve thread blocked reading the socket,
            // so that client would stay connected, never see EOF, and only find
            // out on its next request. shutdown() wakes the read (the thread
            // exits) and sends EOF to the client at once.
            synchronized(activeClients) {
                for (c in ArrayList(activeClients)) disconnect(c)
                activeClients.clear()
            }
        }
    }

    private fun disconnect(client: LocalSocket) {
        try {
            client.shutdownInput()
        } catch (t: Throwable) {
            // Already closed or never connected: nothing to wake.
        }
        try {
            client.shutdownOutput()
        } catch (t: Throwable) {
        }
        closeQuietly(client)
    }

    /**
     * Accepts connections until [stopped]. Each connection is served inline,
     * one at a time (single-in-flight per connection is the contract; serving
     * connections sequentially is sufficient for a synchronous inspector).
     */
    private fun acceptLoop(server: LocalServerSocket, dispatcher: Dispatcher) {
        while (!stopped.get()) {
            val client: LocalSocket =
                try {
                    server.accept()
                } catch (e: IOException) {
                    if (stopped.get()) {
                        // Expected: stop() closed the socket to break us out.
                        break
                    }
                    Log.e(TAG, "accept() failed", e)
                    // Transient accept failure; loop and try again unless stopped.
                    continue
                }

            touchActivity()
            activeClients.add(client)
            Log.i(TAG, "Client connected on @$socketName")
            // Serve each connection on its own thread so a slow/idle/dead client
            // never blocks accepting the next one. Device work inside is
            // serialized by handleLock, so concurrent clients queue safely.
            Thread({
                try {
                    serveConnection(client, dispatcher)
                } catch (t: Throwable) {
                    Log.e(TAG, "Unhandled error serving connection", t)
                } finally {
                    closeQuietly(client)
                    activeClients.remove(client)
                    touchActivity()
                    Log.i(TAG, "Client disconnected from @$socketName")
                }
            }, "ViewSpector-conn").start()
        }
    }

    /**
     * Serves a single connection: loop reading framed Requests, dispatching,
     * and writing framed Responses, until the client closes the stream (clean
     * EOF), a framing/IO error occurs, or a SHUTDOWN command terminates the
     * session. Mirrors SessionHandler.processCommands (SessionHandler.kt:70-93).
     */
    private fun serveConnection(client: LocalSocket, dispatcher: Dispatcher) {
        val input: InputStream = client.inputStream
        val output: OutputStream = client.outputStream

        while (!stopped.get()) {
            val requestBytes: ByteArray =
                try {
                    // Null => clean EOF at a frame boundary: client hung up.
                    Framing.readMessageOrNull(input) ?: run {
                        Log.i(TAG, "Client closed connection (EOF)")
                        return
                    }
                } catch (e: IOException) {
                    Log.i(TAG, "Connection lost while reading request: ${e.message}")
                    return
                } catch (e: IllegalStateException) {
                    // Framing/magic error: unrecoverable for this stream.
                    Log.e(TAG, "Framing error; dropping connection", e)
                    return
                }

            touchActivity()

            val request: ViewInspection.Request =
                try {
                    ViewInspection.Request.parseFrom(requestBytes)
                } catch (t: Throwable) {
                    // Malformed protobuf: reply with a best-effort ERROR (id
                    // unknown, so 0) and keep the connection alive.
                    Log.e(TAG, "Failed to parse Request protobuf", t)
                    writeResponse(output, errorResponse(0, "Malformed request: ${t.message}"))
                    continue
                }

            val isShutdown =
                request.commandCase == ViewInspection.Request.CommandCase.SHUTDOWN
            // Serialize device work across concurrent connections. HELLO and
            // SHUTDOWN touch no UI, so they are answered at once: a client must
            // not wait behind another client's long request (an SKP capture, a
            // big a11y dump) and conclude the app is frozen, and SHUTDOWN must
            // not wait for work it is about to abandon.
            val response: ViewInspection.Response =
                if (isShutdown ||
                    request.commandCase == ViewInspection.Request.CommandCase.HELLO
                ) {
                    dispatcher.handle(request)
                } else {
                    synchronized(handleLock) { dispatcher.handle(request) }
                }

            try {
                writeResponse(output, response)
            } catch (e: IOException) {
                Log.i(TAG, "Connection lost while writing response: ${e.message}")
                if (isShutdown) stop()
                return
            }
            touchActivity()

            // SHUTDOWN: the reply is on its way to the requester; now stop the
            // server, which disconnects every client (this one included).
            if (isShutdown) {
                Log.i(TAG, "SHUTDOWN handled; stopping the server")
                stop()
                return
            }
        }
    }

    private fun wakeAcceptLoop() {
        try {
            LocalSocket().use { it.connect(LocalSocketAddress(socketName)) }
        } catch (e: IOException) {
            // Nothing listening any more: the accept loop has already exited.
        } catch (t: Throwable) {
            Log.w(TAG, "Could not wake the accept loop on @$socketName", t)
        }
    }

    private fun writeResponse(output: OutputStream, response: ViewInspection.Response) {
        Framing.writeMessage(output, response.toByteArray())
    }

    /** A bare ERROR response used when we cannot parse far enough to dispatch. */
    private fun errorResponse(id: Int, message: String): ViewInspection.Response =
        ViewInspection.Response.newBuilder()
            .setId(id)
            .setStatus(ViewInspection.Response.Status.ERROR)
            .setError(message)
            .build()

    /**
     * Spawns a daemon watchdog that closes the server after [IDLE_TIMEOUT_MS]
     * with no client connected and no activity. A connected client counts as
     * activity, so a long-lived host session (the MCP server) is never cut off
     * mid-use; an abandoned injection with nobody connected still goes away.
     * Mirrors the inactivity timeout in ui-inspector Server.kt:62-76.
     */
    private fun startIdleWatchdog(): Thread {
        val watchdog =
            Thread({
                while (!stopped.get()) {
                    try {
                        Thread.sleep(WATCHDOG_TICK_MS)
                    } catch (ie: InterruptedException) {
                        // stop() or run()'s finally interrupted us: exit.
                        return@Thread
                    }
                    if (activeClients.isNotEmpty()) {
                        // Someone is connected: not idle. The disconnect itself
                        // touches the activity clock, so the timeout restarts then.
                        continue
                    }
                    val idleMs = (System.nanoTime() - lastActivityNanos) / 1_000_000L
                    if (idleMs >= IDLE_TIMEOUT_MS) {
                        Log.i(TAG, "Idle for ${idleMs}ms (>= ${IDLE_TIMEOUT_MS}ms); stopping server")
                        stop()
                        return@Thread
                    }
                }
            }, "viewspector-idle-watchdog")
        watchdog.isDaemon = true
        watchdog.start()
        return watchdog
    }

    private fun touchActivity() {
        lastActivityNanos = System.nanoTime()
    }

    private fun closeQuietly(closeable: AutoCloseable?) {
        if (closeable == null) return
        try {
            closeable.close()
        } catch (e: IOException) {
            Log.w(TAG, "Error closing ${closeable.javaClass.simpleName}", e)
        } catch (t: Throwable) {
            Log.w(TAG, "Unexpected error closing ${closeable.javaClass.simpleName}", t)
        }
    }
}
