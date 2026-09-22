package com.mobirent.companion

import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Public Android Device Policy Controller receiver.
 *
 * Used only for legitimate Device Owner / device-admin enrollment via
 * `dpm set-device-owner`. Does not wipe data, delete eSIM profiles, or
 * invoke EuiccManager download APIs.
 */
class DeviceAdminReceiver : android.app.admin.DeviceAdminReceiver() {
    override fun onEnabled(context: Context, intent: Intent) {
        super.onEnabled(context, intent)
        Log.i(TAG, "device admin enabled")
    }

    override fun onDisabled(context: Context, intent: Intent) {
        super.onDisabled(context, intent)
        Log.i(TAG, "device admin disabled")
    }

    companion object {
        private const val TAG = "MobiRentDeviceAdmin"
    }
}
