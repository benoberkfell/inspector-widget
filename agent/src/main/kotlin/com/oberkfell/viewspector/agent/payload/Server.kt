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
import android.os.Process
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

        // accept() failing over and over (fd exhaustion, a broken socket) must not
        // spin a core: back off from 50 ms, doubling to 5 s, reset by a success.
        const val ACCEPT_BACKOFF_MIN_MS = 50L
        const val ACCEPT_BACKOFF_MAX_MS = 5_000L

        // Peers allowed to connect (SO_PEERCRED): adbd runs as shell (2000), or as
        // root (0) after `adb root`; `adb forward` connections come from it. The app's
        // own uid covers in-process callers (stop()'s wake-up connection). Any other
        // app on the device must not be able to read this app's UI.
        const val ROOT_UID = 0
        const val SHELL_UID = 2000

        // Refused peers are logged for the first few, then every Nth, so a hostile
        // client can't flood the log the host tells users to read.
        const val REFUSAL_LOG_FIRST = 5
        const val REFUSAL_LOG_EVERY = 100
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
    // Also the connection cap (WireLimits.MAX_CONNECTIONS): check-and-add under its lock.
    private val activeClients =
        java.util.Collections.synchronizedSet(java.util.HashSet<LocalSocket>())

    // Connections refused by the peer check or the connection cap (log throttling).
    private var refusedPeers = 0
    private var refusedOverCap = 0

    // Names serve threads uniquely (ViewSpector-conn-N).
    private var connectionSeq = 0

    /**
     * Binds the socket and runs the accept loop on the CALLING thread until a
     * shutdown is requested or the idle timeout fires. [Payload.start] invokes
     * this from a dedicated background thread, so it is fine to block here.
     */
    fun run() {
        // SHUTDOWN is carried out by serveConnection, after the reply is written.
        val dispatcher = Dispatcher(handleLock)

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
            // Give every window root its own AccessibilityDelegate back and wake any
            // A11yFocus long-poll (SHUTDOWN, idle stop, or a fatal error).
            A11yEventTap.shutdown()
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
     * Accepts connections until [stopped]. Each admitted connection is served on
     * its own daemon thread (at most [WireLimits.MAX_CONNECTIONS] at once); device
     * work is serialized by handleLock. A connection from a uid other than root,
     * shell or this app is closed unanswered; one over the cap gets an ERROR reply
     * and is closed. Persistent accept() failures back off instead of spinning.
     */
    private fun acceptLoop(server: LocalServerSocket, dispatcher: Dispatcher) {
        var failures = 0
        while (!stopped.get()) {
            val client: LocalSocket =
                try {
                    server.accept()
                } catch (t: Throwable) {
                    if (stopped.get()) {
                        // Expected: stop() closed the socket to break us out.
                        break
                    }
                    failures++
                    val delayMs = acceptBackoffMs(failures)
                    if (failures == 1 || failures % 20 == 0) {
                        Log.e(TAG, "accept() failed ($failures in a row); retrying in ${delayMs}ms", t)
                    }
                    if (!sleepUnlessStopped(delayMs)) break
                    continue
                }
            failures = 0

            if (stopped.get()) {
                // stop()'s wake-up connection (or a late client): nothing to serve.
                closeQuietly(client)
                break
            }
            if (!peerAllowed(client)) {
                closeQuietly(client)
                continue
            }
            if (!admit(client)) continue

            touchActivity()
            Log.i(TAG, "Client connected on @$socketName")
            // Serve each connection on its own thread so a slow/idle/dead client
            // never blocks accepting the next one. Device work inside is
            // serialized by handleLock, so concurrent clients queue safely.
            // Daemon: a lingering client never keeps the app process alive.
            val thread = Thread({
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
            }, "ViewSpector-conn-${++connectionSeq}")
            thread.isDaemon = true
            try {
                thread.start()
            } catch (t: Throwable) {
                // Out of threads/memory: drop this client rather than the server.
                Log.e(TAG, "Could not start a connection thread; closing the client", t)
                activeClients.remove(client)
                closeQuietly(client)
            }
        }
    }

    /** 50 ms after the first failure, doubling, capped at [ACCEPT_BACKOFF_MAX_MS]. */
    private fun acceptBackoffMs(failures: Int): Long {
        val shift = (failures - 1).coerceIn(0, 16)
        return (ACCEPT_BACKOFF_MIN_MS shl shift).coerceAtMost(ACCEPT_BACKOFF_MAX_MS)
    }

    /** Sleeps [ms] unless the server stops first; false when stopped or interrupted. */
    private fun sleepUnlessStopped(ms: Long): Boolean {
        val until = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(ms)
        while (!stopped.get()) {
            val left = TimeUnit.NANOSECONDS.toMillis(until - System.nanoTime())
            if (left <= 0) return true
            try {
                Thread.sleep(minOf(left, 250L))
            } catch (ie: InterruptedException) {
                Thread.currentThread().interrupt()
                return false
            }
        }
        return false
    }

    /**
     * SO_PEERCRED check: only root, shell (adbd, so `adb forward`) and this app's own
     * uid may talk to the agent. Anything else, or credentials that can't be read, is
     * refused (the caller closes the socket without a reply).
     */
    private fun peerAllowed(client: LocalSocket): Boolean {
        val creds =
            try {
                client.peerCredentials
            } catch (t: Throwable) {
                logRefusal("could not read the peer credentials (${t.javaClass.simpleName}: ${t.message})")
                return false
            }
        if (creds == null) {
            logRefusal("the peer credentials are unavailable")
            return false
        }
        val uid = creds.uid
        if (uid == ROOT_UID || uid == SHELL_UID || uid == Process.myUid()) return true
        logRefusal("uid $uid (pid ${creds.pid}) is not root, shell or this app (uid ${Process.myUid()})")
        return false
    }

    private fun logRefusal(why: String) {
        val n = ++refusedPeers
        if (n <= REFUSAL_LOG_FIRST || n % REFUSAL_LOG_EVERY == 0) {
            Log.w(TAG, "Refused a connection on @$socketName: $why (refusal #$n)")
        }
    }

    /**
     * Registers [client] unless [WireLimits.MAX_CONNECTIONS] are already open. Over the
     * cap, the client gets one ERROR frame (id 0, which the host reports as an agent
     * error) and is closed. Returns true when the client was admitted.
     */
    private fun admit(client: LocalSocket): Boolean {
        synchronized(activeClients) {
            if (activeClients.size < WireLimits.MAX_CONNECTIONS) {
                activeClients.add(client)
                return true
            }
        }
        val n = ++refusedOverCap
        if (n <= REFUSAL_LOG_FIRST || n % REFUSAL_LOG_EVERY == 0) {
            Log.w(
                TAG,
                "Refused a connection on @$socketName: ${WireLimits.MAX_CONNECTIONS} clients are " +
                    "already connected (refusal #$n)",
            )
        }
        try {
            writeResponse(
                client.outputStream,
                errorResponse(
                    0,
                    "the agent already serves ${WireLimits.MAX_CONNECTIONS} connections (the " +
                        "most it accepts at once); close an idle inspector-widget session and retry",
                ),
            )
        } catch (t: Throwable) {
            // The client is gone already: nothing to tell it.
        }
        closeQuietly(client)
        return false
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
                } catch (e: Framing.FrameTooLargeException) {
                    // Nothing was allocated or read for it; the stream is out of step.
                    Log.e(
                        TAG,
                        "Request frame of ${e.length} bytes is over the ${e.limit}-byte limit; " +
                            "dropping the connection",
                    )
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
            // not wait for work it is about to abandon. A11Y_FOCUS long-polls
            // first and takes the lock only for its read (Dispatcher).
            val response: ViewInspection.Response =
                if (dispatcher.takesDeviceLock(request)) {
                    synchronized(handleLock) { dispatcher.handle(request) }
                } else {
                    dispatcher.handle(request)
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
