package com.mobirent.companion.protocol

import android.net.LocalServerSocket
import android.net.LocalSocket
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.atomic.AtomicBoolean

class JsonLineServer(
    private val socketName: String,
    private val handler: (JSONObject) -> JSONObject,
) : Thread("companion-$socketName") {
    private val running = AtomicBoolean(true)
    private var server: LocalServerSocket? = null

    override fun run() {
        try {
            server = LocalServerSocket(socketName)
            Log.i(TAG, "listening on localabstract:$socketName")
            while (running.get()) {
                val client = server?.accept() ?: break
                handleClient(client)
            }
        } catch (exc: Exception) {
            if (running.get()) {
                Log.e(TAG, "server $socketName stopped: ${exc.message}")
            }
        }
    }

    private fun handleClient(client: LocalSocket) {
        client.use { socket ->
            val reader = BufferedReader(InputStreamReader(socket.inputStream))
            val line = reader.readLine() ?: return
            val response = try {
                handler(JSONObject(line))
            } catch (exc: Exception) {
                JSONObject(mapOf("success" to false, "error" to (exc.message ?: "invalid request")))
            }
            socket.outputStream.write((response.toString() + "\n").toByteArray(Charsets.UTF_8))
            socket.outputStream.flush()
        }
    }

    fun shutdown() {
        running.set(false)
        try {
            server?.close()
        } catch (_: Exception) {
        }
    }

    companion object {
        private const val TAG = "CompanionSocket"
    }
}
