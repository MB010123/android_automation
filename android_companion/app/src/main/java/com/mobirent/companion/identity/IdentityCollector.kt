package com.mobirent.companion.identity

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.provider.Settings
import android.telephony.TelephonyManager
import android.telephony.euicc.EuiccManager
import com.mobirent.companion.BuildConfig
import com.mobirent.companion.esim.EsimState
import com.mobirent.companion.storage.LocalStore

class IdentityCollector(
    private val context: Context,
    private val store: LocalStore,
) {
    fun identity(): DeviceIdentity {
        val serial = try {
            Build.getSerial().takeIf { it.isNotBlank() && it != Build.UNKNOWN }
        } catch (_: SecurityException) {
            null
        }
        return DeviceIdentity(
            deviceId = Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID),
            model = Build.MODEL,
            manufacturer = Build.MANUFACTURER,
            androidVersion = Build.VERSION.RELEASE,
            sdkInt = Build.VERSION.SDK_INT,
            adbSerial = serial,
            assignedSlot = store.assignedSlot,
            appVersion = BuildConfig.VERSION_NAME,
            euiccSupported = hasEuicc(),
        )
    }

    fun health(esimState: EsimState): DeviceHealth {
        val identity = identity()
        val cm = context.getSystemService(ConnectivityManager::class.java)
        val network = cm.activeNetwork
        val caps = network?.let { cm.getNetworkCapabilities(it) }
        val transport = when {
            caps == null -> "none"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            else -> "other"
        }
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val percent = if (level != null && scale != null && level >= 0 && scale > 0) {
            (level * 100) / scale
        } else {
            null
        }
        val status = battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL
        val simReady = try {
            context.getSystemService(TelephonyManager::class.java)?.simState ==
                TelephonyManager.SIM_STATE_READY
        } catch (_: SecurityException) {
            false
        }
        return DeviceHealth(
            deviceId = identity.deviceId,
            slotId = identity.assignedSlot,
            networkConnected = caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true,
            transport = transport,
            batteryPercent = percent,
            charging = charging,
            simReady = simReady,
            esimState = esimState.wireValue,
            appHealthy = true,
        )
    }

    fun hasEuicc(): Boolean {
        if (!context.packageManager.hasSystemFeature("android.hardware.telephony.euicc")) {
            return false
        }
        val manager = context.getSystemService(EuiccManager::class.java)
        return manager?.isEnabled == true
    }
}
