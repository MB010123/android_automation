package com.mobirent.companion

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.VpnService
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.mobirent.companion.backend.BackendClient
import com.mobirent.companion.esim.EsimController
import com.mobirent.companion.identity.IdentityCollector
import com.mobirent.companion.identity.ImeiCollector
import com.mobirent.companion.protocol.CommandRouter
import com.mobirent.companion.protocol.JsonLineServer
import com.mobirent.companion.vpn.CompanionVpnService
import org.json.JSONObject

class CompanionForegroundService : Service() {
    private val servers = mutableListOf<JsonLineServer>()
    private lateinit var router: CommandRouter
    private val backend = BackendClient()

    override fun onCreate() {
        super.onCreate()
        val store = CompanionApp.instance.store
        val identity = IdentityCollector(this, store)
        val esim = EsimController(this, identity)
        router = CommandRouter(
            assignedSlot = { store.assignedSlot },
            assignSlot = { store.assignedSlot = it },
            identity = { identity.identity().toMap() },
            health = {
                esim.status()
                identity.health(esim.state).toMap()
            },
            esimStatus = { esim.status() },
            cachedJob = { store.cachedJobResult(it) },
            cacheJob = { jobId, result -> store.cacheJobResult(jobId, result.toString()) },
            vpnStatus = {
                mapOf(
                    "active" to CompanionVpnService.active,
                    "dry_run" to !BuildConfig.PRODUCTION_PROXY_ENABLED,
                    "consent_required" to (VpnService.prepare(this) != null),
                )
            },
            startTestVpn = { startTestVpn() },
            stopVpn = { stopTestVpn() },
            imeiAccess = { ImeiCollector(this).access() },
            canSilentInstall = { esim.canSilentInstall() },
            realEsimEnabled = BuildConfig.REAL_ESIM_ENABLED,
            productionProxyEnabled = BuildConfig.PRODUCTION_PROXY_ENABLED,
            liveDownloadSlotId = 1,
            downloadEsim = { request -> esim.downloadFromHostRequest(request) },
            switchEsim = { request -> esim.switchFromHostRequest(request) },
        )
        listOf(SOCKET_COMPANION, SOCKET_PROVISIONING, SOCKET_NETWORK).forEach { name ->
            val server = JsonLineServer(name) { request -> router.handle(request) }
            servers += server
            server.start()
        }
        backend.register(identity.identity().toMap())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val pending = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, CompanionApp.CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(getString(R.string.notification_text))
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
        startForeground(NOTIFICATION_ID, notification)
        return START_STICKY
    }

    override fun onDestroy() {
        servers.forEach { it.shutdown() }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startTestVpn(): Map<String, Any?> {
        val prepare = VpnService.prepare(this)
        if (prepare != null) {
            return mapOf(
                "success" to false,
                "active" to false,
                "consent_required" to true,
                "error" to "user must grant VPN permission once from the companion activity",
            )
        }
        startService(Intent(this, CompanionVpnService::class.java))
        return mapOf("success" to true, "active" to true, "dry_run" to true)
    }

    private fun stopTestVpn(): Map<String, Any?> {
        startService(Intent(this, CompanionVpnService::class.java).setAction(CompanionVpnService.ACTION_STOP))
        return mapOf("success" to true, "active" to false)
    }

    companion object {
        const val SOCKET_COMPANION = "mobi_rent.companion"
        const val SOCKET_PROVISIONING = "mobi_rent.provisioning"
        const val SOCKET_NETWORK = "mobi_rent.network"
        private const val NOTIFICATION_ID = 42

        fun start(context: Context) {
            context.startForegroundService(Intent(context, CompanionForegroundService::class.java))
        }
    }
}
