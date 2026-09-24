package com.example.deeplock.lockmode

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import com.example.deeplock.MainActivity
import com.example.deeplock.R
import com.example.deeplock.data.SettingsRepository
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

@AndroidEntryPoint
class BootReminderReceiver : BroadcastReceiver() {
    @Inject lateinit var settings: SettingsRepository
    @Inject lateinit var notifications: LockNotificationController
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != Intent.ACTION_BOOT_COMPLETED) return
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                if (settings.settings.first().lockReviewEnabled) {
                    settings.setRuntimeStatus("STOPPED", System.currentTimeMillis())
                    notifications.createChannels()
                    val openIntent = Intent(context, MainActivity::class.java)
                        .putExtra(LockNotificationController.EXTRA_DESTINATION, LockNotificationController.DEST_SETTINGS)
                    val open = PendingIntent.getActivity(context, 31, openIntent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                    context.getSystemService(NotificationManager::class.java).notify(
                        LockNotificationController.BOOT_REMINDER_NOTIFICATION_ID,
                        NotificationCompat.Builder(context, LockNotificationController.STATUS_CHANNEL)
                            .setSmallIcon(R.drawable.ic_deeplock_tile)
                            .setContentTitle("Bật lại Lock Review")
                            .setContentText("Android yêu cầu bạn mở DeepLock sau khi khởi động lại.")
                            .setContentIntent(open).setAutoCancel(true).build())
                }
            } finally { pending.finish() }
        }
    }
}
