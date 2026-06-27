package com.hermes.phoneagent

import android.app.*
import android.content.Intent
import android.os.IBinder
import android.util.Log
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.OutputStreamWriter
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Foreground service that runs a TCP socket server on localhost.
 * Connected clients receive JSON events from EventBus.
 *
 * SECURITY:
 * - Binds to 127.0.0.1 ONLY — no network-accessible port.
 * - First message from client must be {"type":"auth","token":"<session_token>"}.
 * - Unauthenticated connections are closed after 5 seconds.
 * - No data is written to disk or logged at INFO level.
 */
class EventSocketService : Service(), EventBus.Listener {

    private val running = AtomicBoolean(false)
    private var serverThread: Thread? = null
    private var serverSocket: ServerSocket? = null
    private val clients = CopyOnWriteArrayList<ClientConnection>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForegroundWithNotification()
        EventBus.register(this)
        startServer()
    }

    override fun onDestroy() {
        super.onDestroy()
        EventBus.unregister(this)
        stopServer()
    }

    override fun onEvent(event: JSONObject) {
        val line = event.toString() + "\n"
        val deadClients = mutableListOf<ClientConnection>()
        for (client in clients) {
            if (client.authenticated) {
                try {
                    client.writeLine(line)
                } catch (e: Exception) {
                    deadClients.add(client)
                }
            }
        }
        for (dead in deadClients) {
            dead.close()
            clients.remove(dead)
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
                val reader = socket.getInputStream().bufferedReader()
                val firstLine = reader.readLine()
                if (firstLine == null) {
                    client.close()
                    clients.remove(client)
                    return@Thread
                }
                val msg = JSONObject(firstLine)
                val expectedToken = EventBus.sessionToken
                if (msg.optString("type") == "auth"
                    && expectedToken != null
                    && msg.optString("token") == expectedToken
                ) {
                    client.authenticated = true
                    socket.soTimeout = 0  // Remove timeout after auth.
                    Log.i(TAG, "Client authenticated")
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

    companion object {
        private const val TAG = "HermesSocket"
        private const val PORT = 18765
        private const val AUTH_TIMEOUT_MS = 5000
        private const val NOTIFICATION_ID = 1
    }
}

/**
 * Wrapper around a connected client socket.
 */
class ClientConnection(private val socket: Socket) {
    @Volatile
    var authenticated = false
    private val writer: BufferedWriter =
        BufferedWriter(OutputStreamWriter(socket.getOutputStream()))
    private val writeLock = Any()

    fun writeLine(line: String) {
        synchronized(writeLock) {
            writer.write(line)
            writer.flush()
        }
    }

    fun close() {
        try { socket.close() } catch (_: Exception) {}
    }
}
