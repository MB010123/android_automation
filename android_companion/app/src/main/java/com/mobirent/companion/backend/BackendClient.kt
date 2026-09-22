package com.mobirent.companion.backend

import android.util.Log
import com.mobirent.companion.BuildConfig
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.URL
import javax.net.ssl.HttpsURLConnection

/**
 * Optional HTTPS client for companion registration/heartbeat.
 *
 * Disabled by default: farm phones currently have no cellular/Wi-Fi path,
 * and Phase 1 already heartbeats from the host daemon. This client never
 * logs tokens and refuses to run without HTTPS + a compiled token.
 */
class BackendClient {
    fun register(payload: Map<String, Any?>): Boolean = post("/companion/register", payload)

    fun heartbeat(payload: Map<String, Any?>): Boolean = post("/companion/heartbeat", payload)

    private fun post(path: String, payload: Map<String, Any?>): Boolean {
        if (!BuildConfig.BACKEND_ENABLED) {
            Log.i(TAG, "backend disabled (Phase 1 host heartbeat remains the source of truth)")
            return false
        }
        val base = BuildConfig.BACKEND_BASE_URL
        val token = BuildConfig.DEVICE_TOKEN
        if (!base.startsWith("https://") || token.isBlank()) {
            Log.w(TAG, "backend skipped: missing HTTPS base URL or device token")
            return false
        }
        val connection = URL(base.trimEnd('/') + path).openConnection() as HttpsURLConnection
        return try {
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 8_000
            OutputStreamWriter(connection.outputStream).use { writer ->
                writer.write(JSONObject(payload).toString())
            }
            connection.responseCode in 200..299
        } catch (exc: Exception) {
            Log.w(TAG, "backend request failed: ${exc.javaClass.simpleName}")
            false
        } finally {
            connection.disconnect()
        }
    }

    companion object {
        private const val TAG = "CompanionBackend"
    }
}
