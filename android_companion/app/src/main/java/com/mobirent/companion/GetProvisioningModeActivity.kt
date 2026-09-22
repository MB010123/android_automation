package com.mobirent.companion

import android.app.Activity
import android.app.admin.DevicePolicyManager
import android.content.Intent
import android.os.Bundle

/**
 * Public Android Device Owner provisioning handler.
 *
 * The system launches this during QR / NFC / zero-touch enrollment
 * *before* the device is owned. It does not factory-reset, wipe, or
 * call EuiccManager. PhoneFarmBox production devices must not be
 * enrolled through this path without a separate explicit authorization.
 */
class GetProvisioningModeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val result = Intent()
        result.putExtra(
            DevicePolicyManager.EXTRA_PROVISIONING_MODE,
            DevicePolicyManager.PROVISIONING_MODE_FULLY_MANAGED_DEVICE,
        )
        setResult(RESULT_OK, result)
        finish()
    }
}
