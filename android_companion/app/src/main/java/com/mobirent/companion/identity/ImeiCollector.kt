package com.mobirent.companion.identity

import android.content.Context
import android.telephony.TelephonyManager
import android.util.Log

/**
 * Probe IMEI / IMEI2 through the official TelephonyManager APIs.
 *
 * On Android 10+ (including 16), [TelephonyManager.getImei] requires
 * READ_PRIVILEGED_PHONE_STATE. A normal companion app is expected to
 * receive SecurityException. Values are never written to logcat.
 */
class ImeiCollector(private val context: Context) {
    fun access(): Map<String, Any?> {
        val telephony = try {
            context.getSystemService(TelephonyManager::class.java)
        } catch (_: Exception) {
            null
        }
        if (telephony == null) {
            return mapOf(
                "imei1_accessible" to false,
                "imei2_accessible" to false,
                "imei1" to null,
                "imei2" to null,
                "source" to "TelephonyManager.getImei",
                "error" to "TelephonyManager unavailable",
                "esim_imei_slot" to 1,
            )
        }
        val first = readSlot(telephony, 0)
        val second = readSlot(telephony, 1)
        return mapOf(
            "imei1_accessible" to first.accessible,
            "imei2_accessible" to second.accessible,
            "imei1" to first.value,
            "imei2" to second.value,
            "source" to "TelephonyManager.getImei",
            "error" to listOfNotNull(first.error, second.error).distinct().joinToString("; ").ifBlank { null },
            "esim_imei_slot" to 1,
            "privilege_required" to "READ_PRIVILEGED_PHONE_STATE",
        )
    }

    private fun readSlot(telephony: TelephonyManager, slotIndex: Int): SlotRead {
        return try {
            val value = telephony.getImei(slotIndex)
            if (value.isNullOrBlank()) {
                SlotRead(false, null, "empty")
            } else {
                SlotRead(true, value, null)
            }
        } catch (exc: SecurityException) {
            Log.i(TAG, "getImei($slotIndex) denied: ${exc.javaClass.simpleName}")
            SlotRead(false, null, "SecurityException")
        } catch (exc: Exception) {
            Log.i(TAG, "getImei($slotIndex) failed: ${exc.javaClass.simpleName}")
            SlotRead(false, null, exc.javaClass.simpleName)
        }
    }

    private data class SlotRead(val accessible: Boolean, val value: String?, val error: String?)

    companion object {
        private const val TAG = "CompanionImei"
    }
}
