package com.example.deeplock.lockmode

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.example.deeplock.MainActivity
import com.example.deeplock.R
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

internal object LegacyNotificationCleanup {
    val learningChannelIds = setOf(
        LockNotificationController.LEGACY_CARD_CHANNEL,
        LockNotificationController.LEGACY_LEARNING_CHANNEL,
    )
    val channelIdsToDelete = learningChannelIds + LockNotificationController.LEGACY_MONITOR_CHANNEL
    val knownLearningNotificationIds = setOf(LockNotificationController.LEGACY_LEARNING_NOTIFICATION_ID)

    fun shouldCancel(channelId: String?, notificationId: Int): Boolean =
        notificationId in knownLearningNotificationIds || channelId in learningChannelIds
}

@Singleton
class LockNotificationController @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private val manager: NotificationManager get() = context.getSystemService(NotificationManager::class.java)

    fun createChannels() {
        cancelLearning()
        if (Build.VERSION.SDK_INT < 26) return
        val status = NotificationChannel(STATUS_CHANNEL, "Trạng thái DeepLock", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Foreground service do người dùng chủ động bật"
            setSound(null, null)
            enableVibration(false)
            lockscreenVisibility = Notification.VISIBILITY_PRIVATE
        }
        manager.createNotificationChannel(status)
        // Learning payload notifications were removed. Delete every historical
        // channel during in-place upgrades and keep only the quiet FGS status.
        LegacyNotificationCleanup.channelIdsToDelete.forEach(manager::deleteNotificationChannel)
    }

    private fun notificationsGloballyAvailable(): Boolean {
        val permission = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        val enabled = NotificationManagerCompat.from(context).areNotificationsEnabled()
        return permission && enabled
    }

    private fun channelAvailable(channelId: String): Boolean =
        Build.VERSION.SDK_INT < 26 || manager.getNotificationChannel(channelId)?.importance?.let { importance ->
            importance != NotificationManager.IMPORTANCE_NONE
        } == true

    /** The foreground service only depends on its quiet, mandatory status channel. */
    fun statusNotificationsAvailable(): Boolean =
        notificationsGloballyAvailable() && channelAvailable(STATUS_CHANNEL)

    fun serviceNotification(
        paused: Boolean = false,
        setupRequired: Boolean = false,
    ): Notification {
        createChannels()
        val open = activityPendingIntent(
            requestCode = 100,
            atomId = null,
            questionId = null,
            sessionId = null,
            destination = if (setupRequired) DEST_SETTINGS else DEST_HOME,
        )
        val pause = servicePendingIntent(101, LockMonitorService.ACTION_PAUSE)
        val disable = servicePendingIntent(102, LockMonitorService.ACTION_DISABLE)
        return NotificationCompat.Builder(context, STATUS_CHANNEL)
            .setSmallIcon(R.drawable.ic_deeplock_tile)
            .setContentTitle(when {
                setupRequired -> "DeepLock cần kiểm tra bảng trực tiếp"
                paused -> "DeepLock đang tạm dừng"
                else -> "DeepLock đang hoạt động"
            })
            .setContentText(when {
                setupRequired -> "Kiểm tra quyền Hiện trên cùng và thiết lập One UI"
                paused -> "Chạm Tile để tiếp tục"
                else -> "Chờ chu kỳ bật màn hình · hoàn toàn offline"
            })
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setContentIntent(open)
            .addAction(0, "Tạm dừng 1 giờ", pause)
            .addAction(0, "Tắt", disable)
            .build()
    }

    fun cancelLearning() {
        LegacyNotificationCleanup.knownLearningNotificationIds.forEach { manager.cancel(it) }
        if (Build.VERSION.SDK_INT < 26) return
        // Some pre-v1 builds used different IDs. Channel ownership lets an
        // in-place upgrade remove those cards without touching status/reminder.
        runCatching {
            manager.activeNotifications
                .filter { active ->
                    LegacyNotificationCleanup.shouldCancel(
                        channelId = active.notification.channelId,
                        notificationId = active.id,
                    )
                }
                .forEach { active -> manager.cancel(active.tag, active.id) }
        }
    }

    fun cancelBootReminder() = manager.cancel(BOOT_REMINDER_NOTIFICATION_ID)

    private fun activityPendingIntent(
        requestCode: Int,
        atomId: String?,
        questionId: String?,
        sessionId: String?,
        destination: String,
    ): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(EXTRA_DESTINATION, destination)
            .putExtra(EXTRA_ATOM_ID, atomId)
            .putExtra(EXTRA_QUESTION_ID, questionId)
            .putExtra(EXTRA_UNLOCK_SESSION_ID, sessionId)
        return PendingIntent.getActivity(context, requestCode, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun servicePendingIntent(requestCode: Int, action: String): PendingIntent =
        PendingIntent.getService(
            context,
            requestCode,
            Intent(context, LockMonitorService::class.java).setAction(action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    companion object {
        const val STATUS_CHANNEL = "deeplock_status"
        const val LEGACY_CARD_CHANNEL = "deeplock_cards"
        const val LEGACY_LEARNING_CHANNEL = "deeplock_learning"
        const val LEGACY_MONITOR_CHANNEL = "deeplock_monitor"
        const val STATUS_NOTIFICATION_ID = 1001
        const val BOOT_REMINDER_NOTIFICATION_ID = 1002
        const val LEGACY_LEARNING_NOTIFICATION_ID = 2001
        const val EXTRA_DESTINATION = "deeplock_destination"
        const val EXTRA_PACK_ID = "content_pack_id"
        const val EXTRA_ATOM_ID = "atom_id"
        const val EXTRA_QUESTION_ID = "question_id"
        const val EXTRA_UNLOCK_SESSION_ID = "unlock_session_id"
        const val DEST_HOME = "HOME"
        const val DEST_LESSON = "LESSON"
        const val DEST_QUIZ = "QUIZ"
        const val DEST_SETTINGS = "SETTINGS"
    }
}
