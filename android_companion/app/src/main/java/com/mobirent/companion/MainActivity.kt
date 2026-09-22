package com.mobirent.companion

import android.content.Intent
import android.net.VpnService
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.mobirent.companion.esim.EsimController
import com.mobirent.companion.identity.IdentityCollector
import com.mobirent.companion.vpn.CompanionVpnService
import org.json.JSONObject

class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        intent.getIntExtra(EXTRA_SLOT, -1).takeIf { it in 1..20 }?.let {
            CompanionApp.instance.store.assignedSlot = it
        }
        findViewById<Button>(R.id.startService).setOnClickListener {
            CompanionForegroundService.start(this)
            refresh()
        }
        findViewById<Button>(R.id.grantVpn).setOnClickListener {
            val prepare = VpnService.prepare(this)
            if (prepare != null) {
                startActivityForResult(prepare, REQ_VPN)
            } else {
                startService(Intent(this, CompanionVpnService::class.java))
                refresh()
            }
        }
        CompanionForegroundService.start(this)
        refresh()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    @Deprecated("Required for VpnService.prepare result")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_VPN && resultCode == RESULT_OK) {
            startService(Intent(this, CompanionVpnService::class.java))
        }
        refresh()
    }

    private fun refresh() {
        val store = CompanionApp.instance.store
        val identity = IdentityCollector(this, store)
        val esim = EsimController(this, identity)
        val body = JSONObject(identity.identity().toMap() + identity.health(esim.state).toMap() + esim.status())
        findViewById<TextView>(R.id.status).text = body.toString(2)
    }

    companion object {
        const val EXTRA_SLOT = "slot_id"
        private const val REQ_VPN = 1001
    }
}
