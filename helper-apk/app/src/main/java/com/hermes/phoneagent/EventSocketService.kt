package com.hermes.phoneagent

import android.app.*
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.os.IBinder
import android.util.Log
import android.util.Base64
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.OutputStreamWriter
import java.net.ServerSocket
import java.net.Socket
import java.security.SecureRandom
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Foreground service that runs a TCP socket server on localhost.
 * Connected clients receive JSON events from EventBus.
 *
 * SECURITY:
 * - Binds to 127.0.0.1 ONLY — no network-accessible port.
 * - Host and helper exchange nonces and mutually prove possession of the
 *   ADB-issued session token with HMAC-SHA256. The token never crosses the
 *   socket, and events are withheld until both proofs pass.
 * - Unauthenticated connections are closed after 5 seconds.
 * - No data is written to disk or logged at INFO level.
 */
class EventSocketService : Service(), EventBus.Listener {

    private val running = AtomicBoolean(false)
    private var serverThread: Thread? = null
    private var serverSocket: ServerSocket? = null
    private val clients = CopyOnWriteArrayList<ClientConnection>()
    private val eventWriter = Executors.newSingleThreadExecutor { task ->
        Thread(task, "event-socket-writer").apply { isDaemon = true }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_SET_CLIPBOARD) {
            setClipboard(intent.getStringExtra("text_b64"))
            return START_NOT_STICKY
        }
        val token = intent?.getStringExtra("token")
        if (!token.isNullOrBlank() && token.length >= 32) {
            EventBus.setToken(token)
            Log.i(TAG, "Session token updated (${token.length} chars)")
            if (!running.get()) {
                startServer()
            }
        } else {
            Log.w(TAG, "Service start without a valid session token")
        }
        return START_NOT_STICKY
    }

    private fun setClipboard(encodedText: String?) {
        if (encodedText.isNullOrBlank() || encodedText.length > MAX_CLIPBOARD_B64) {
            Log.w(TAG, "Rejected invalid clipboard payload")
            return
        }
        try {
            val bytes = Base64.decode(encodedText, Base64.NO_WRAP)
            val text = bytes.toString(Charsets.UTF_8)
            val clipboard = getSystemService(ClipboardManager::class.java)
            clipboard.setPrimaryClip(ClipData.newPlainText("", text))
            Log.i(TAG, "Clipboard updated (${text.length} chars)")
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "Rejected malformed clipboard payload")
        }
    }

    override fun onCreate() {
        super.onCreate()
        startForegroundWithNotification()
        EventBus.register(this)
    }

    override fun onDestroy() {
        super.onDestroy()
        EventBus.unregister(this)
        stopServer()
        eventWriter.shutdownNow()
    }

    override fun onEvent(event: JSONObject) {
        val line = event.toString() + "\n"
        eventWriter.execute {
            val deadClients = mutableListOf<ClientConnection>()
            for (client in clients) {
                if (client.authenticated) {
                    try {
                        client.writeLine(line)
                        Log.d(TAG, "Forwarded ${event.optString("type")} event")
                    } catch (e: Exception) {
                        Log.w(TAG, "Client write failed", e)
                        deadClients.add(client)
                    }
                }
            }
            for (dead in deadClients) {
                dead.close()
                clients.remove(dead)
            }
        }
    }

    private fun startServer() {
        running.set(true)
        serverThread = Thread({
            try {
                serverSocket = ServerSocket(PORT, 2, java.net.InetAddress.getByName("127.0.0.1"))
                Log.i(TAG, "Event socket server listening on 127.0.0.1:$PORT")
                while (running.get()) {
                    try {
                        val socket = serverSocket?.accept() ?: break
                        handleNewClient(socket)
                    } catch (e: Exception) {
                        if (running.get()) Log.w(TAG, "Accept error", e)
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server socket error", e)
            }
        }, "event-socket-server").apply { isDaemon = true; start() }
    }

    private fun stopServer() {
        running.set(false)
        try { serverSocket?.close() } catch (_: Exception) {}
        for (client in clients) {
            try { client.close() } catch (_: Exception) {}
        }
        clients.clear()
    }

    private fun handleNewClient(socket: Socket) {
        val client = ClientConnection(socket)
        clients.add(client)

        // Authentication must complete within 5 seconds.
        Thread({
            try {
                socket.soTimeout = AUTH_TIMEOUT_MS
                val firstLine = client.readLine()
                if (firstLine == null) {
                    client.close()
                    clients.remove(client)
                    return@Thread
                }
                val msg = JSONObject(firstLine)
                val sessionToken = EventBus.sessionToken
                val nonce = msg.optString("nonce")
                if (msg.optString("type") == "auth_challenge"
                    && sessionToken != null
                    && nonce.length in 32..256
                ) {
                    val helperNonceBytes = ByteArray(32).also {
                        SecureRandom().nextBytes(it)
                    }
                    val helperNonce = helperNonceBytes.joinToString("") {
                        "%02x".format(it.toInt() and 0xff)
                    }
                    client.writeLine(
                        JSONObject()
                            .put("type", "auth_proof")
                            .put("nonce", helperNonce)
                            .put("proof", hmacHex(sessionToken, "helper:$nonce"))
                            .toString() + "\n"
                    )
                    val responseLine = client.readLine()
                    val response = if (responseLine == null) null else JSONObject(responseLine)
                    val expectedResponse = hmacHex(sessionToken, "host:$helperNonce")
                    if (response?.optString("type") == "auth_response"
                        && constantTimeEquals(response.optString("proof"), expectedResponse)
                    ) {
                        client.authenticated = true
                        client.writeLine(JSONObject().put("type", "auth_ok").toString() + "\n")
                        PhoneNotificationListener.replayPendingFriendRequests()
                        socket.soTimeout = 0  // Remove timeout after auth.
                        Log.i(TAG, "Client authenticated")
                    } else {
                        Log.w(TAG, "Client proof failed — closing")
                        client.close()
                        clients.remove(client)
                    }
                } else {
                    Log.w(TAG, "Client auth failed — closing")
                    client.close()
                    clients.remove(client)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Client auth error", e)
                client.close()
                clients.remove(client)
            }
        }, "client-auth").apply { isDaemon = true; start() }
    }

    private fun startForegroundWithNotification() {
        val channelId = "hermes_phone_agent"
        val channel = NotificationChannel(
            channelId,
            "Hermes Phone Agent",
            NotificationManager.IMPORTANCE_LOW,
        )
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)

        val notification = Notification.Builder(this, channelId)
            .setContentTitle("Hermes Phone Agent")
            .setContentText("Monitoring phone events")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    private fun hmacHex(token: String, value: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(token.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(value.toByteArray(Charsets.UTF_8)).joinToString("") {
            "%02x".format(it.toInt() and 0xff)
        }
    }

    private fun constantTimeEquals(left: String, right: String): Boolean {
        if (left.length != right.length) return false
        var difference = 0
        for (index in left.indices) {
            difference = difference or (left[index].code xor right[index].code)
        }
        return difference == 0
    }

    companion object {
        private const val TAG = "HermesSocket"
        private const val PORT = 18765
        private const val AUTH_TIMEOUT_MS = 5000
        private const val NOTIFICATION_ID = 1
        private const val ACTION_SET_CLIPBOARD = "com.hermes.phoneagent.SET_CLIPBOARD"
        private const val MAX_CLIPBOARD_B64 = 4096
    }
}

/**
 * Wrapper around a connected client socket.
 */
class ClientConnection(private val socket: Socket) {
    @Volatile
    var authenticated = false
    // Keep the reader alive for the lifetime of the connection. If the
    // temporary authentication reader is collected, Android may close its
    // underlying SocketInputStream and silently disconnect an idle client.
    private val reader = socket.getInputStream().bufferedReader()
    private val writer: BufferedWriter =
        BufferedWriter(OutputStreamWriter(socket.getOutputStream()))
    private val writeLock = Any()

    fun writeLine(line: String) {
        synchronized(writeLock) {
            writer.write(line)
            writer.flush()
        }
    }

    fun readLine(): String? = reader.readLine()

    fun close() {
        try { socket.close() } catch (_: Exception) {}
    }
}
