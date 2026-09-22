package com.mobirent.companion.esim

/**
 * Parses [EuiccManager.downloadSubscription] / switch PendingIntent results.
 *
 * Android delivers the public result as
 * `android.telephony.euicc.extra.EMBEDDED_SUBSCRIPTION_RESULT` when present.
 * Some LPAs only put the code on [android.app.PendingIntent.send]'s resultCode.
 * A missing extra must not be treated as -1 (that collides with Activity.RESULT_OK
 * and EuiccService.RESULT_MUST_DEACTIVATE_SIM).
 */
object EuiccCallbackResult {
    const val EXTRA_RESULT = "android.telephony.euicc.extra.EMBEDDED_SUBSCRIPTION_RESULT"
    const val RESULT_OK = 0
    const val RESULT_RESOLVABLE = 1
    const val RESULT_ERROR = 2

    data class Parsed(
        val resultCode: Int?,
        val source: String,
    ) {
        val isOk: Boolean get() = resultCode == RESULT_OK
        val isResolvable: Boolean get() = resultCode == RESULT_RESOLVABLE
        val isError: Boolean get() = resultCode == RESULT_ERROR
        val isMissing: Boolean get() = resultCode == null
    }

    fun parse(hasResultExtra: Boolean, extraValue: Int, broadcastResultCode: Int?): Parsed {
        if (hasResultExtra) {
            return Parsed(extraValue, "extra")
        }
        if (broadcastResultCode != null && broadcastResultCode in VALID_EUICC_MANAGER_CODES) {
            return Parsed(broadcastResultCode, "broadcast")
        }
        return Parsed(null, "missing")
    }

    private val VALID_EUICC_MANAGER_CODES = setOf(RESULT_OK, RESULT_RESOLVABLE, RESULT_ERROR)
}
