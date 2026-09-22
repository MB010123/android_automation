package com.mobirent.companion

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import com.mobirent.companion.storage.LocalStore

class CompanionApp : Application() {
    lateinit var store: LocalStore
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        store = LocalStore(this)
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel),
            NotificationManager.IMPORTANCE_LOW,
        )
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    companion object {
        const val CHANNEL_ID = "companion_host_link"
        lateinit var instance: CompanionApp
            private set
    }
}
