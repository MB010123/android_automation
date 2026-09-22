package com.mobirent.companion.vpn

import android.content.Intent
import android.net.VpnService
import android.os.ParcelFileDescriptor
import android.util.Log

/**
 * Test-mode VpnService. It establishes a dummy TUN so we can confirm the
 * Android VPN consent path works. It does not apply SOCKS5 credentials or
 * route production traffic.
 *
 * Android has no system-wide SOCKS5 setting. A production implementation
 * would need tun2socks inside this process, always-on VPN, and
 * block-on-disconnect — none of which are enabled in this build.
 */
class CompanionVpnService : VpnService() {
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopTestSession()
            else -> startTestSession()
        }
        return START_STICKY
    }

    private fun startTestSession() {
        if (tun != null) return
        try {
            tun = Builder()
                .setSession("Mobi-Rent test VPN")
                .addAddress("10.0.0.2", 32)
                .addRoute("0.0.0.0", 0)
                .establish()
            active = tun != null
            Log.i(TAG, "test VPN ${if (active) "started" else "not established (consent missing)"}")
        } catch (exc: Exception) {
            active = false
            Log.w(TAG, "test VPN failed: ${exc.message}")
        }
    }

    private fun stopTestSession() {
        try {
            tun?.close()
        } catch (_: Exception) {
        }
        tun = null
        active = false
        stopSelf()
    }

    override fun onDestroy() {
        stopTestSession()
        super.onDestroy()
    }

    companion object {
        const val ACTION_STOP = "com.mobirent.companion.vpn.STOP"
        private const val TAG = "CompanionVpn"
        @Volatile var active: Boolean = false
            private set
        private var tun: ParcelFileDescriptor? = null
    }
}
