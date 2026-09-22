package com.mobirent.companion.protocol

import com.google.common.truth.Truth.assertThat
import org.json.JSONObject
import org.junit.Test

class CommandRouterTest {
    private val jobs = mutableMapOf<String, JSONObject>()
    private var slot: Int? = 1

    private fun router(
        realEsim: Boolean = false,
        productionProxy: Boolean = false,
        silentInstall: Boolean = false,
        download: ((JSONObject) -> JSONObject)? = null,
        switchEsim: ((JSONObject) -> JSONObject)? = null,
    ) = CommandRouter(
        assignedSlot = { slot },
        assignSlot = { slot = it },
        identity = { mapOf("device_id" to "abc", "slot_id" to slot) },
        health = { mapOf("app_healthy" to true, "slot_id" to slot) },
        esimStatus = { mapOf("esim_state" to "dry_run", "euicc_supported" to true) },
        cachedJob = { jobs[it] },
        cacheJob = { id, result -> jobs[id] = result },
        vpnStatus = { mapOf("active" to false) },
        startTestVpn = { mapOf("active" to true, "dry_run" to true) },
        stopVpn = { mapOf("active" to false) },
        imeiAccess = { mapOf("imei1_accessible" to false, "imei2_accessible" to false, "error" to "SecurityException") },
        canSilentInstall = { silentInstall },
        realEsimEnabled = realEsim,
        productionProxyEnabled = productionProxy,
        downloadEsim = download,
        switchEsim = switchEsim,
    )

    @Test
    fun pingAndIdentity() {
        val result = router().handle(JSONObject(mapOf("command" to "get_identity")))
        assertThat(result.getBoolean("success")).isTrue()
        assertThat(result.getString("device_id")).isEqualTo("abc")
    }

    @Test
    fun rejectsCommandForAnotherSlot() {
        val result = router().handle(
            JSONObject(mapOf("command" to "get_health", "slot_id" to 9)),
        )
        assertThat(result.getBoolean("success")).isFalse()
        assertThat(result.getString("error")).contains("does not match")
    }

    @Test
    fun dryRunProvisionDoesNotClaimSuccessAndIsIdempotent() {
        val request = JSONObject(
            mapOf(
                "command" to "provision_esim",
                "job_id" to "job-1",
                "slot_id" to 1,
                "activation_code" to "LPA:1\$example",
            ),
        )
        val first = router().handle(request)
        val second = router().handle(request)
        assertThat(first.getBoolean("success")).isFalse()
        assertThat(first.getString("error")).contains("dry_run")
        assertThat(second.toString()).isEqualTo(first.toString())
    }

    @Test
    fun socks5StaysInactiveInDryRun() {
        val result = router().handle(
            JSONObject(mapOf("command" to "ensure_socks5_route", "slot_id" to 1)),
        )
        assertThat(result.getBoolean("success")).isTrue()
        assertThat(result.getBoolean("active")).isFalse()
        assertThat(result.getBoolean("dry_run")).isTrue()
    }

    @Test
    fun assignSlotPersists() {
        val result = router().handle(JSONObject(mapOf("command" to "assign_slot", "slot_id" to 4)))
        assertThat(result.getBoolean("success")).isTrue()
        assertThat(slot).isEqualTo(4)
        val identity = router().handle(JSONObject(mapOf("command" to "get_identity")))
        assertThat(identity.get("slot_id")).isEqualTo(4)
    }

    @Test
    fun unknownCommandIsRejected() {
        val result = router().handle(JSONObject(mapOf("command" to "wipe_device")))
        assertThat(result.getBoolean("success")).isFalse()
        assertThat(result.getString("error")).contains("unknown command")
    }

    @Test
    fun productionProxyFlagStillRefusesImplementation() {
        val result = router(productionProxy = true).handle(
            JSONObject(mapOf("command" to "ensure_socks5_route", "slot_id" to 1)),
        )
        assertThat(result.getBoolean("success")).isFalse()
        assertThat(result.getString("error")).contains("not implemented")
    }

    @Test
    fun realEsimWithoutPrivilegesRefusesSilentInstallAndDoesNotEchoCode() {
        val request = JSONObject(
            mapOf(
                "command" to "provision_esim",
                "job_id" to "job-priv",
                "slot_id" to 1,
                "activation_code" to "LPA:1\$t-mobile.example\$SECRET",
            ),
        )
        val result = router(realEsim = true, silentInstall = false).handle(request)
        assertThat(result.getBoolean("success")).isFalse()
        assertThat(result.getString("esim_state")).isEqualTo("requires_user_action")
        assertThat(result.getBoolean("can_silent_install")).isFalse()
        assertThat(result.toString()).doesNotContain("SECRET")
        assertThat(result.toString()).doesNotContain("activation_code")
    }

    @Test
    fun liveDownloadIsRestrictedToSlot1() {
        slot = 2
        val captured = mutableListOf<JSONObject>()
        val request = JSONObject(
            mapOf(
                "command" to "provision_esim",
                "job_id" to "job-slot2",
                "slot_id" to 2,
                "activation_code" to "LPA:1\$t-mobile.example\$SECRET",
            ),
        )
        val result = router(
            realEsim = true,
            silentInstall = true,
            download = {
                captured.add(it)
                JSONObject(mapOf("success" to true, "activation_code" to "SECRET"))
            },
        ).handle(request)
        assertThat(result.getBoolean("success")).isFalse()
        assertThat(result.getString("error")).contains("restricted to slot 1")
        assertThat(captured).isEmpty()
        assertThat(result.toString()).doesNotContain("SECRET")
    }

    @Test
    fun liveDownloadCallbackDoesNotCacheOrEchoActivationCode() {
        val request = JSONObject(
            mapOf(
                "command" to "provision_esim",
                "job_id" to "job-live",
                "slot_id" to 1,
                "activation_code" to "LPA:1\$t-mobile.example\$SECRET",
                "switch_after_download" to false,
            ),
        )
        val result = router(
            realEsim = true,
            silentInstall = true,
            download = { incoming ->
                assertThat(incoming.optString("activation_code")).isEqualTo("LPA:1\$t-mobile.example\$SECRET")
                JSONObject(
                    mapOf(
                        "success" to true,
                        "job_id" to "job-live",
                        "slot_id" to 1,
                        "device_code" to 0,
                        "esim_state" to "installed",
                        "activation_code" to "SECRET",
                    ),
                )
            },
        ).handle(request)
        assertThat(result.getBoolean("success")).isTrue()
        assertThat(result.getInt("device_code")).isEqualTo(0)
        assertThat(result.toString()).doesNotContain("SECRET")
        assertThat(result.has("activation_code")).isFalse()
        assertThat(jobs.getValue("job-live").toString()).doesNotContain("SECRET")
    }

    @Test
    fun switchEsimDoesNotRequireOrEchoActivationCode() {
        val request = JSONObject(
            mapOf(
                "command" to "switch_esim",
                "job_id" to "job-switch",
                "slot_id" to 1,
                "subscription_id" to 12,
            ),
        )
        val result = router(
            realEsim = true,
            silentInstall = true,
            switchEsim = { incoming ->
                assertThat(incoming.has("activation_code")).isFalse()
                JSONObject(
                    mapOf(
                        "success" to true,
                        "job_id" to "job-switch",
                        "slot_id" to 1,
                        "device_code" to 0,
                        "esim_state" to "enabled",
                        "activation_code" to "SECRET",
                    ),
                )
            },
        ).handle(request)
        assertThat(result.getBoolean("success")).isTrue()
        assertThat(result.getString("esim_state")).isEqualTo("enabled")
        assertThat(result.toString()).doesNotContain("SECRET")
        assertThat(result.has("activation_code")).isFalse()
    }

    @Test
    fun switchEsimIsRestrictedToSlot1() {
        slot = 2
        val captured = mutableListOf<JSONObject>()
        val result = router(
            realEsim = true,
            silentInstall = true,
            switchEsim = {
                captured.add(it)
                JSONObject(mapOf("success" to true))
            },
        ).handle(
            JSONObject(
                mapOf(
                    "command" to "switch_esim",
                    "job_id" to "job-switch-2",
                    "slot_id" to 2,
                    "subscription_id" to 12,
                ),
            ),
        )
        assertThat(result.getBoolean("success")).isFalse()
        assertThat(result.getString("error")).contains("restricted to slot 1")
        assertThat(captured).isEmpty()
    }

    @Test
    fun identityDoesNotIncludeImeiAndAccessCommandReportsDenial() {
        val identity = router().handle(JSONObject(mapOf("command" to "get_identity")))
        assertThat(identity.has("imei1")).isFalse()
        assertThat(identity.has("imei2")).isFalse()
        val access = router().handle(JSONObject(mapOf("command" to "get_imei_access")))
        assertThat(access.getBoolean("success")).isTrue()
        assertThat(access.getBoolean("imei1_accessible")).isFalse()
        assertThat(access.getBoolean("imei2_accessible")).isFalse()
    }
}
