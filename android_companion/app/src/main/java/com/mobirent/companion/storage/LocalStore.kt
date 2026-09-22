package com.mobirent.companion.storage

import android.content.Context
import org.json.JSONObject

/**
 * Local persistence for non-secret runtime state.
 * Activation codes and proxy passwords are never written here.
 */
class LocalStore(context: Context) {
    private val prefs = context.getSharedPreferences("companion_state", Context.MODE_PRIVATE)

    var assignedSlot: Int?
        get() {
            val value = prefs.getInt(KEY_SLOT, -1)
            return if (value in 1..20) value else null
        }
        set(value) {
            if (value == null) {
                prefs.edit().remove(KEY_SLOT).apply()
            } else {
                require(value in 1..20) { "slot_id must be 1-20" }
                prefs.edit().putInt(KEY_SLOT, value).apply()
            }
        }

    fun cacheJobResult(jobId: String, resultJson: String) {
        require(jobId.isNotBlank())
        val cache = JSONObject(prefs.getString(KEY_JOBS, "{}") ?: "{}")
        cache.put(jobId, JSONObject(resultJson))
        prefs.edit().putString(KEY_JOBS, cache.toString()).apply()
    }

    fun cachedJobResult(jobId: String): JSONObject? {
        val cache = JSONObject(prefs.getString(KEY_JOBS, "{}") ?: "{}")
        return cache.optJSONObject(jobId)
    }

    companion object {
        private const val KEY_SLOT = "assigned_slot"
        private const val KEY_JOBS = "job_results"
    }
}
