package com.mobirent.companion.identity

data class DeviceIdentity(
    val deviceId: String,
    val model: String,
    val manufacturer: String,
    val androidVersion: String,
    val sdkInt: Int,
    val adbSerial: String?,
    val assignedSlot: Int?,
    val appVersion: String,
    val euiccSupported: Boolean,
) {
    fun toMap(): Map<String, Any?> = mapOf(
        "device_id" to deviceId,
        "model" to model,
        "manufacturer" to manufacturer,
        "android_version" to androidVersion,
        "sdk_int" to sdkInt,
        "adb_serial" to adbSerial,
        "slot_id" to assignedSlot,
        "app_version" to appVersion,
        "euicc_supported" to euiccSupported,
    )
}

data class DeviceHealth(
    val deviceId: String,
    val slotId: Int?,
    val networkConnected: Boolean,
    val transport: String,
    val batteryPercent: Int?,
    val charging: Boolean?,
    val simReady: Boolean,
    val esimState: String,
    val appHealthy: Boolean,
) {
    fun toMap(): Map<String, Any?> = mapOf(
        "device_id" to deviceId,
        "slot_id" to slotId,
        "network_connected" to networkConnected,
        "transport" to transport,
        "battery_percent" to batteryPercent,
        "charging" to charging,
        "sim_ready" to simReady,
        "esim_state" to esimState,
        "app_healthy" to appHealthy,
    )
}
