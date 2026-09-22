package com.mobirent.companion.protocol

import org.json.JSONObject

/**
 * Host JSON-line command router. Framework-free so it can be unit-tested
 * without an Android device. Privileged eSIM download and production SOCKS5
 * are refused unless the corresponding flags are enabled.
 */
class CommandRouter(
    private val assignedSlot: () -> Int?,
    private val assignSlot: (Int) -> Unit,
    private val identity: () -> Map<String, Any?>,
    private val health: () -> Map<String, Any?>,
    private val esimStatus: () -> Map<String, Any?>,
    private val cachedJob: (String) -> JSONObject?,
    private val cacheJob: (String, JSONObject) -> Unit,
    private val vpnStatus: () -> Map<String, Any?>,
    private val startTestVpn: () -> Map<String, Any?>,
    private val stopVpn: () -> Map<String, Any?>,
    private val imeiAccess: () -> Map<String, Any?> = { emptyMap() },
    private val canSilentInstall: () -> Boolean = { false },
    private val realEsimEnabled: Boolean = false,
    private val productionProxyEnabled: Boolean = false,
    private val liveDownloadSlotId: Int = 1,
    private val downloadEsim: ((JSONObject) -> JSONObject)? = null,
    private val switchEsim: ((JSONObject) -> JSONObject)? = null,
) {
    fun handle(request: JSONObject): JSONObject {
        val command = request.optString("command")
        if (command.isBlank()) {
            return error("missing command")
        }
        val requestSlot = request.optInt("slot_id", -1).takeIf { it in 1..20 }
        val localSlot = assignedSlot()
        if (
            command != "assign_slot" &&
            requestSlot != null &&
            localSlot != null &&
            requestSlot != localSlot
        ) {
            return error("command slot_id $requestSlot does not match this device slot $localSlot")
        }
        return when (command) {
            "ping" -> ok(mapOf("pong" to true))
            "get_identity" -> ok(identity())
            "get_health" -> ok(health())
            "get_esim_status" -> ok(esimStatus())
            "assign_slot" -> assign(request)
            "provision_esim" -> provision(request)
            "switch_esim" -> switchProfile(request)
            "ensure_socks5_route" -> socks(request)
            "start_test_vpn" -> ok(startTestVpn())
            "stop_vpn" -> ok(stopVpn())
            "vpn_status" -> ok(vpnStatus())
            "get_imei_access" -> ok(imeiAccess())
            else -> error("unknown command: $command")
        }
    }

    private fun assign(request: JSONObject): JSONObject {
        val slot = request.optInt("slot_id", -1)
        if (slot !in 1..20) return error("slot_id must be 1-20")
        assignSlot(slot)
        return ok(mapOf("slot_id" to slot))
    }

    private fun provision(request: JSONObject): JSONObject {
        val jobId = request.optString("job_id")
        if (jobId.isBlank()) return error("job_id must not be empty")
        cachedJob(jobId)?.let { return it }

        val slot = request.optInt("slot_id", -1)
        if (slot !in 1..20) return error("slot_id must be 1-20")
        // Activation codes are accepted on the wire but never cached or logged.
        // The code is forwarded to downloadEsim only after every gate passes.

        val result = when {
            !realEsimEnabled -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "device_code" to -1,
                    "esim_state" to "dry_run",
                    "error" to "dry_run: real eSIM install disabled",
                ),
            )
            slot != liveDownloadSlotId -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "device_code" to -1,
                    "esim_state" to "failed",
                    "error" to "live download is restricted to slot $liveDownloadSlotId",
                ),
            )
            !canSilentInstall() -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "device_code" to -1,
                    "esim_state" to "requires_user_action",
                    "can_silent_install" to false,
                    "error" to "companion lacks WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, and Device Owner; silent eSIM install refused",
                ),
            )
            downloadEsim == null -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "device_code" to -1,
                    "esim_state" to "failed",
                    "error" to "silent downloadSubscription is not enabled in this companion build",
                ),
            )
            else -> downloadEsim.invoke(request)
        }
        cacheJob(jobId, stripActivationCode(result))
        return stripActivationCode(result)
    }

    private fun switchProfile(request: JSONObject): JSONObject {
        val jobId = request.optString("job_id")
        if (jobId.isBlank()) return error("job_id must not be empty")
        cachedJob(jobId)?.let { return it }
        val slot = request.optInt("slot_id", -1)
        if (slot !in 1..20) return error("slot_id must be 1-20")
        val result = when {
            !realEsimEnabled -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "esim_state" to "dry_run",
                    "error" to "dry_run: real eSIM switch disabled",
                ),
            )
            slot != liveDownloadSlotId -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "esim_state" to "failed",
                    "error" to "live switch is restricted to slot $liveDownloadSlotId",
                ),
            )
            !canSilentInstall() -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "esim_state" to "requires_user_action",
                    "error" to "companion lacks WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, and Device Owner; silent eSIM switch refused",
                ),
            )
            switchEsim == null -> json(
                mapOf(
                    "success" to false,
                    "job_id" to jobId,
                    "slot_id" to slot,
                    "esim_state" to "failed",
                    "error" to "switchToSubscription is not enabled in this companion build",
                ),
            )
            else -> switchEsim.invoke(request)
        }
        cacheJob(jobId, stripActivationCode(result))
        return stripActivationCode(result)
    }

    private fun socks(request: JSONObject): JSONObject {
        val slot = request.optInt("slot_id", -1)
        if (slot !in 1..20) return error("slot_id must be 1-20")
        if (!productionProxyEnabled) {
            return json(
                mapOf(
                    "success" to true,
                    "active" to false,
                    "dry_run" to true,
                    "slot_id" to slot,
                    "error" to "dry_run: production SOCKS5 disabled; use start_test_vpn",
                ),
            )
        }
        return error("production SOCKS5 is not implemented in this build")
    }

    private fun ok(payload: Map<String, Any?>): JSONObject =
        json(mapOf("success" to true) + payload)

    private fun error(message: String): JSONObject =
        json(mapOf("success" to false, "error" to message))

    private fun json(payload: Map<String, Any?>): JSONObject {
        val out = JSONObject()
        payload.forEach { (key, value) -> out.put(key, value ?: JSONObject.NULL) }
        return out
    }

    private fun stripActivationCode(result: JSONObject): JSONObject {
        result.remove("activation_code")
        result.remove("qr_url")
        return result
    }
}
