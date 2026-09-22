package com.mobirent.companion

import android.app.Activity
import android.os.Bundle

/**
 * Public Android Device Owner policy-compliance handler.
 *
 * Called after Device Owner provisioning completes. Applies no wipe,
 * lock, password, or eSIM-delete policies. Returns success so Android
 * can finish enrollment of an isolated test device.
 */
class AdminPolicyComplianceActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setResult(RESULT_OK)
        finish()
    }
}
