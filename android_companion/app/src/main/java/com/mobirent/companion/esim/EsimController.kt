package com.mobirent.companion.esim

import android.app.PendingIntent
import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.telephony.TelephonyManager
import android.telephony.euicc.DownloadableSubscription
import android.telephony.euicc.EuiccManager
import com.mobirent.companion.BuildConfig
import com.mobirent.companion.identity.IdentityCollector
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * eUICC capability probe and public [EuiccManager.downloadSubscription] path.
 *
 * Silent download is allowed only with a legitimate Android authority:
 * WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, or Device Owner.
 * User-consent / resolvable UI is treated as failure. Activation codes are
 * never logged or persisted. Profile deletion is not implemented.
 */
class EsimController(
    private val context: Context,
    private val identity: IdentityCollector,
) {
    @Volatile
    var state: EsimState = EsimState.READY
        private set

    fun canSilentInstall(): Boolean = privileges()["can_silent_install"] == true

    fun isDeviceOwner(): Boolean {
        return try {
            context.getSystemService(DevicePolicyManager::class.java)
                ?.isDeviceOwnerApp(context.packageName) == true
        } catch (_: SecurityException) {
            false
        }
    }

    fun privileges(): Map<String, Any?> {
        val writeEmbedded = context.checkSelfPermission(PERM_WRITE_EMBEDDED) ==
            PackageManager.PERMISSION_GRANTED
        val carrier = try {
            context.getSystemService(TelephonyManager::class.java)?.hasCarrierPrivileges() == true
        } catch (_: SecurityException) {
            false
        }
        val deviceOwner = isDeviceOwner()
        val canSilent = writeEmbedded || carrier || deviceOwner
        return mapOf(
            "has_write_embedded_subscriptions" to writeEmbedded,
            "has_carrier_privileges" to carrier,
            "device_owner" to deviceOwner,
            "can_silent_install" to canSilent,
            "requires_user_consent" to !canSilent,
            "privilege_required" to "WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, " +
                "or Device Owner; user-consent UI is not used",
        )
    }

    fun status(): Map<String, Any?> {
        val supported = identity.hasEuicc()
        if (!supported) {
            state = EsimState.UNSUPPORTED
        } else if (!canSilentInstall() && state == EsimState.READY) {
            state = if (BuildConfig.REAL_ESIM_ENABLED) {
                EsimState.REQUIRES_USER_ACTION
            } else {
                EsimState.DRY_RUN
            }
        }
        val manager = context.getSystemService(EuiccManager::class.java)
        val telephony = try {
            context.getSystemService(TelephonyManager::class.java)
        } catch (_: SecurityException) {
            null
        }
        val simState = try {
            telephony?.simState
        } catch (_: SecurityException) {
            null
        }
        return mapOf(
            "esim_state" to state.wireValue,
            "euicc_supported" to supported,
            "euicc_enabled" to (manager?.isEnabled == true),
            "real_esim_enabled" to BuildConfig.REAL_ESIM_ENABLED,
            "sim_state" to simStateName(simState),
            "network_operator" to (telephony?.networkOperatorName ?: ""),
        ) + privileges()
    }

    fun downloadFromHostRequest(request: JSONObject, timeoutMs: Long = 180_000L): JSONObject {
        val jobId = request.optString("job_id")
        val slot = request.optInt("slot_id", -1)
        val activationCode = request.optString("activation_code").trim()
        val requestedSwitch = request.optBoolean("switch_after_download", false)
        if (slot != LIVE_SLOT_ID) {
            return jsonResult(
                success = false,
                jobId = jobId,
                slot = slot,
                deviceCode = null,
                error = "live download is restricted to slot $LIVE_SLOT_ID",
            )
        }
        if (!BuildConfig.REAL_ESIM_ENABLED) {
            return jsonResult(false, jobId, slot, null, "dry_run: real eSIM install disabled")
        }
        if (!canSilentInstall()) {
            return jsonResult(
                false,
                jobId,
                slot,
                null,
                "companion lacks WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, and Device Owner",
            )
        }
        if (activationCode.isEmpty()) {
            return jsonResult(false, jobId, slot, null, "activation_code missing")
        }
        // Device Owner may pass switchAfterDownload into downloadSubscription.
        // That does not grant switchToSubscription / profile erase.
        val switchAfter = requestedSwitch && canSilentInstall()
        return downloadBlocking(jobId, slot, activationCode, switchAfter, timeoutMs)
    }

    fun switchFromHostRequest(request: JSONObject, timeoutMs: Long = 180_000L): JSONObject {
        val jobId = request.optString("job_id")
        val slot = request.optInt("slot_id", -1)
        val subscriptionId = request.optInt("subscription_id", Int.MIN_VALUE)
        if (slot != LIVE_SLOT_ID) {
            return jsonResult(
                success = false,
                jobId = jobId,
                slot = slot,
                deviceCode = null,
                error = "live switch is restricted to slot $LIVE_SLOT_ID",
            )
        }
        if (!BuildConfig.REAL_ESIM_ENABLED) {
            return jsonResult(false, jobId, slot, null, "dry_run: real eSIM switch disabled")
        }
        if (!canSilentInstall()) {
            return jsonResult(
                false,
                jobId,
                slot,
                null,
                "companion lacks WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, and Device Owner",
            )
        }
        if (subscriptionId == Int.MIN_VALUE || subscriptionId < 0) {
            return jsonResult(false, jobId, slot, null, "subscription_id missing")
        }
        return switchBlocking(jobId, slot, subscriptionId, timeoutMs)
    }

    private fun downloadBlocking(
        jobId: String,
        slot: Int,
        activationCode: String,
        switchAfterDownload: Boolean,
        timeoutMs: Long,
    ): JSONObject {
        val manager = context.getSystemService(EuiccManager::class.java)
            ?: return jsonResult(false, jobId, slot, null, "EuiccManager unavailable")
        if (manager.isEnabled != true) {
            return jsonResult(false, jobId, slot, null, "EuiccManager not enabled")
        }
        state = EsimState.PROVISIONING
        val subscription = DownloadableSubscription.forActivationCode(activationCode)
        return awaitEuiccPending(
            jobId = jobId,
            slot = slot,
            action = ACTION_DOWNLOAD_RESULT,
            timeoutMs = timeoutMs,
            opName = "download",
            successState = "installed",
        ) { pending ->
            manager.downloadSubscription(subscription, switchAfterDownload, pending)
        }
    }

    private fun switchBlocking(
        jobId: String,
        slot: Int,
        subscriptionId: Int,
        timeoutMs: Long,
    ): JSONObject {
        val manager = context.getSystemService(EuiccManager::class.java)
            ?: return jsonResult(false, jobId, slot, null, "EuiccManager unavailable")
        if (manager.isEnabled != true) {
            return jsonResult(false, jobId, slot, null, "EuiccManager not enabled")
        }
        state = EsimState.PROVISIONING
        return awaitEuiccPending(
            jobId = jobId,
            slot = slot,
            action = ACTION_SWITCH_RESULT,
            timeoutMs = timeoutMs,
            opName = "switch",
            successState = "enabled",
        ) { pending ->
            manager.switchToSubscription(subscriptionId, pending)
        }
    }

    private fun awaitEuiccPending(
        jobId: String,
        slot: Int,
        action: String,
        timeoutMs: Long,
        opName: String,
        successState: String,
        invoke: (PendingIntent) -> Unit,
    ): JSONObject {
        val latch = CountDownLatch(1)
        val callback = AtomicReference<CallbackSnapshot?>()
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context?, intent: Intent?) {
                callback.set(CallbackSnapshot(intent, resultCode))
                latch.countDown()
            }
        }
        val filter = IntentFilter(action)
        if (Build.VERSION.SDK_INT >= 33) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(receiver, filter)
        }
        val pending = PendingIntent.getBroadcast(
            context,
            jobId.hashCode(),
            Intent(action).setPackage(context.packageName),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
        )
        return try {
            invoke(pending)
            val completed = latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            if (!completed) {
                state = EsimState.FAILED
                return jsonResult(false, jobId, slot, null, "EuiccManager $opName timed out")
            }
            mapCallback(jobId, slot, callback.get(), opName, successState)
        } catch (exc: SecurityException) {
            state = EsimState.FAILED
            jsonResult(false, jobId, slot, null, "SecurityException: ${sanitize(exc.message)}")
        } catch (exc: Exception) {
            state = EsimState.FAILED
            jsonResult(false, jobId, slot, null, sanitize(exc.message))
        } finally {
            try {
                context.unregisterReceiver(receiver)
            } catch (_: Exception) {
            }
        }
    }

    private fun mapCallback(
        jobId: String,
        slot: Int,
        snapshot: CallbackSnapshot?,
        opName: String,
        successState: String,
    ): JSONObject {
        val intent = snapshot?.intent
        val parsed = EuiccCallbackResult.parse(
            hasResultExtra = intent?.hasExtra(EuiccCallbackResult.EXTRA_RESULT) == true,
            extraValue = intent?.getIntExtra(EuiccCallbackResult.EXTRA_RESULT, Int.MIN_VALUE)
                ?: Int.MIN_VALUE,
            broadcastResultCode = snapshot?.broadcastResultCode,
        )
        return when {
            parsed.isOk -> {
                state = EsimState.INSTALLED
                jsonResult(true, jobId, slot, parsed.resultCode, null, successState)
            }
            parsed.isResolvable -> {
                state = EsimState.REQUIRES_USER_ACTION
                jsonResult(
                    false,
                    jobId,
                    slot,
                    parsed.resultCode,
                    "resolvable EuiccManager $opName result; user-consent UI is not used",
                    "requires_user_action",
                )
            }
            parsed.isMissing -> {
                state = EsimState.FAILED
                jsonResult(
                    false,
                    jobId,
                    slot,
                    null,
                    "EuiccManager $opName callback missing ${EuiccCallbackResult.EXTRA_RESULT}",
                    "failed",
                )
            }
            else -> {
                state = EsimState.FAILED
                jsonResult(
                    false,
                    jobId,
                    slot,
                    parsed.resultCode,
                    "EuiccManager $opName failed result=${parsed.resultCode}",
                    "failed",
                )
            }
        }
    }

    private fun jsonResult(
        success: Boolean,
        jobId: String,
        slot: Int,
        deviceCode: Int?,
        error: String?,
        esimState: String = if (success) "installed" else "failed",
    ): JSONObject {
        val out = JSONObject()
        out.put("success", success)
        out.put("job_id", jobId)
        out.put("slot_id", slot)
        if (deviceCode == null) {
            out.put("device_code", JSONObject.NULL)
        } else {
            out.put("device_code", deviceCode)
        }
        out.put("esim_state", esimState)
        if (error != null) out.put("error", error)
        return out
    }

    private fun simStateName(state: Int?): String = when (state) {
        TelephonyManager.SIM_STATE_ABSENT -> "absent"
        TelephonyManager.SIM_STATE_READY -> "ready"
        TelephonyManager.SIM_STATE_UNKNOWN -> "unknown"
        TelephonyManager.SIM_STATE_PIN_REQUIRED -> "pin_required"
        TelephonyManager.SIM_STATE_PUK_REQUIRED -> "puk_required"
        TelephonyManager.SIM_STATE_NETWORK_LOCKED -> "network_locked"
        TelephonyManager.SIM_STATE_NOT_READY -> "not_ready"
        TelephonyManager.SIM_STATE_PERM_DISABLED -> "perm_disabled"
        TelephonyManager.SIM_STATE_CARD_IO_ERROR -> "card_io_error"
        TelephonyManager.SIM_STATE_CARD_RESTRICTED -> "card_restricted"
        null -> "unavailable"
        else -> "other"
    }

    private data class CallbackSnapshot(
        val intent: Intent?,
        val broadcastResultCode: Int,
    )

    companion object {
        const val PERM_WRITE_EMBEDDED = "android.permission.WRITE_EMBEDDED_SUBSCRIPTIONS"
        const val LIVE_SLOT_ID = 1
        private const val ACTION_DOWNLOAD_RESULT = "com.mobirent.companion.action.EUICC_DOWNLOAD_RESULT"
        private const val ACTION_SWITCH_RESULT = "com.mobirent.companion.action.EUICC_SWITCH_RESULT"

        fun sanitize(message: String?): String {
            val raw = message ?: "error"
            return raw.replace(Regex("LPA:\\S+"), "LPA:<redacted>").take(240)
        }
    }
}
