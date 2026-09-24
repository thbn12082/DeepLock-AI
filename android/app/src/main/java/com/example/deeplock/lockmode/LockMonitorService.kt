package com.example.deeplock.lockmode

import android.app.KeyguardManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.view.Display
import androidx.core.content.ContextCompat
import com.example.deeplock.data.SettingsRepository
import com.example.deeplock.data.StudyCoordinator
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

@AndroidEntryPoint
class LockMonitorService : Service() {
    @Inject lateinit var coordinator: StudyCoordinator
    @Inject lateinit var notifications: LockNotificationController
    @Inject lateinit var settings: SettingsRepository
    @Inject lateinit var directLauncher: DirectLockActivityLauncher

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val events = Channel<ScreenSignal>(Channel.UNLIMITED)
    private val postMutex = Mutex()
    private val delayedPosts = ConcurrentHashMap<String, Job>()
    private var explicitlyStopped = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val signal = when (intent?.action) {
                Intent.ACTION_SCREEN_OFF -> ScreenSignal.Off
                Intent.ACTION_SCREEN_ON -> ScreenSignal.On
                Intent.ACTION_USER_PRESENT -> ScreenSignal.UserPresent
                else -> null
            }
            signal?.let(events::trySend)
        }
    }

    override fun onCreate() {
        super.onCreate()
        notifications.createChannels()
        notifications.cancelBootReminder()
        startForeground(LockNotificationController.STATUS_NOTIFICATION_ID, notifications.serviceNotification())
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, filter, RECEIVER_NOT_EXPORTED)
        else @Suppress("DEPRECATION") registerReceiver(receiver, filter)

        scope.launch {
            if (!notifications.statusNotificationsAvailable()) {
                coordinator.setAppOnly("status_notification_permission_or_channel_at_start")
                stopSelf()
                return@launch
            }
            try {
                if (!directLauncher.hasRequiredSpecialAccess()) {
                    // Keep the required status FGS alive so the setup screen and
                    // runtime diagnostics remain truthful. Direct-only mode must
                    // not silently degrade into learning notification 2001.
                    coordinator.setAppOnly("direct_special_access_missing_at_start")
                    notifications.cancelLearning()
                } else {
                    coordinator.enableLockReview()
                }
            } catch (error: Throwable) {
                coordinator.setAppOnly("content_or_start_failure:${error.javaClass.simpleName}")
                stopSelf()
                return@launch
            }
            val runtimeSettings = settings.settings.first()
            val paused = runtimeSettings.runtimeStatus == "PAUSED"
            val setupRequired = runtimeSettings.runtimeStatus == "APP_ONLY" &&
                !directLauncher.hasRequiredSpecialAccess()
            startForeground(
                LockNotificationController.STATUS_NOTIFICATION_ID,
                notifications.serviceNotification(paused, setupRequired),
            )
            if (displayIsInteractiveAndOn()) {
                coordinator.recoverPendingSession()?.let { postSession(it) }
            }
            for (signal in events) handle(signal)
        }
        scope.launch {
            while (isActive) {
                delay(HEARTBEAT_INTERVAL_MILLIS)
                coordinator.heartbeat()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_DISABLE -> scope.launch {
                postMutex.withLock {
                    explicitlyStopped = true
                    cancelDelayedPosts()
                    coordinator.disableLockReview()
                    settings.setLockEnabled(false)
                    notifications.cancelLearning()
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf()
                }
            }
            ACTION_PAUSE -> scope.launch {
                postMutex.withLock {
                    cancelDelayedPosts()
                    coordinator.pauseFor(PAUSE_MILLIS)
                    notifications.cancelLearning()
                    startForeground(LockNotificationController.STATUS_NOTIFICATION_ID, notifications.serviceNotification(paused = true))
                }
            }
            ACTION_RESUME -> scope.launch {
                if (!notifications.statusNotificationsAvailable()) {
                    coordinator.setAppOnly("status_notification_permission_or_channel_on_resume")
                    stopSelf()
                } else if (!directLauncher.hasRequiredSpecialAccess()) {
                    coordinator.setAppOnly("direct_special_access_missing_on_resume")
                    notifications.cancelLearning()
                    startForeground(
                        LockNotificationController.STATUS_NOTIFICATION_ID,
                        notifications.serviceNotification(setupRequired = true),
                    )
                } else {
                    coordinator.enableLockReview()
                    startForeground(LockNotificationController.STATUS_NOTIFICATION_ID, notifications.serviceNotification())
                    if (displayIsInteractiveAndOn()) coordinator.recoverPendingSession()?.let { postSession(it) }
                }
            }
            ACTION_CARD_OPENED -> scope.launch {
                coordinator.markNotificationOpened(intent.getStringExtra(LockNotificationController.EXTRA_UNLOCK_SESSION_ID))
                notifications.cancelLearning()
            }
            ACTION_CARD_DISMISSED -> notifications.cancelLearning()
        }
        return START_STICKY
    }

    private suspend fun handle(signal: ScreenSignal) {
        when (signal) {
            ScreenSignal.Off -> {
                // If the user relocks during the fast-unlock grace window, finish
                // the first cycle before arming the next one. Each physical cycle
                // still owns exactly one persisted session/notify call.
                val pendingIds = delayedPosts.keys.toList()
                pendingIds.forEach { id ->
                    delayedPosts.remove(id)?.cancel()
                    postSession(id)
                }
                val appSettings = settings.settingsValue()
                if (appSettings.lockReviewEnabled &&
                    appSettings.runtimeStatus == "APP_ONLY" &&
                    directLauncher.hasRequiredSpecialAccess()
                ) {
                    // Access may have been granted from Android Settings rather
                    // than our Activity-result callback. Recover before arming
                    // this real screen-off edge so the very next wake can show.
                    runCatching { coordinator.enableLockReview() }
                }
                coordinator.onScreenOff()
            }
            ScreenSignal.On -> {
                // ACTION_SCREEN_ON follows the interactive-state transition, but
                // One UI can report the default display as DOZE for a few more
                // frames. Wait only while already interactive; never wake AOD.
                if (!awaitInteractiveDisplayOn()) return
                if (!notifications.statusNotificationsAvailable()) {
                    coordinator.setAppOnly("status_notification_permission_or_channel")
                    notifications.cancelLearning()
                    stopSelf()
                    return
                }
                val appSettings = settings.settingsValue()
                if (!directLauncher.hasRequiredSpecialAccess()) {
                    // Check before creating a session so a revoked/missing setup
                    // permission never creates a study session with no surface.
                    coordinator.setAppOnly("direct_special_access_missing_on_screen_on")
                    notifications.cancelLearning()
                    startForeground(
                        LockNotificationController.STATUS_NOTIFICATION_ID,
                        notifications.serviceNotification(setupRequired = true),
                    )
                    return
                }
                if (appSettings.lockReviewEnabled &&
                    appSettings.runtimeStatus == "APP_ONLY"
                ) {
                    // Also recovers when access was granted from Android Settings
                    // instead of through this app's activity-result callback.
                    val recovered = runCatching { coordinator.enableLockReview() }.isSuccess
                    if (!recovered) {
                        coordinator.setAppOnly("direct_setup_recovery_failed")
                        return
                    }
                    // If access was granted while the display was already off,
                    // APP_ONLY could not record that edge. Arm the same physical
                    // cycle now, before consuming its real SCREEN_ON signal.
                    coordinator.onScreenOff()
                    startForeground(
                        LockNotificationController.STATUS_NOTIFICATION_ID,
                        notifications.serviceNotification(),
                    )
                }
                val keyguard = getSystemService(KeyguardManager::class.java)
                val sessionId = coordinator.onScreenOn(
                    interactive = getSystemService(PowerManager::class.java).isInteractive,
                    displayOn = defaultDisplayOn(),
                    keyguardLocked = keyguard.isKeyguardLocked,
                ) ?: return
                delayedPosts[sessionId]?.cancel()
                val deliveryTiming = LockDeliveryTimingPolicy.forScreenOn(
                    directPrimaryEnabled = true,
                )
                if (deliveryTiming.startDelayMillis == 0L) {
                    // There is no artificial biometric grace for the primary
                    // surface: attempt the exact persisted card as soon as a real
                    // STATE_ON (not AOD) cycle has been accepted.
                    postSession(sessionId)
                } else {
                    delayedPosts[sessionId] = scope.launch {
                        delay(deliveryTiming.startDelayMillis)
                        if (!getSystemService(KeyguardManager::class.java).isKeyguardLocked) {
                            coordinator.onUserPresent()
                        }
                        postSession(sessionId)
                    }
                }
            }
            ScreenSignal.UserPresent -> {
                postMutex.withLock {
                    val sessionId = coordinator.onUserPresent() ?: return@withLock
                    delayedPosts.remove(sessionId)?.cancel()
                    postSessionLocked(sessionId)
                }
            }
        }
    }

    private suspend fun postSession(sessionId: String) = postMutex.withLock {
        postSessionLocked(sessionId)
    }

    private suspend fun postSessionLocked(sessionId: String) {
        withContext(NonCancellable) {
            val card = coordinator.pendingCard(sessionId) ?: return@withContext
            var delivery = DirectLaunchResult(
                decision = DirectFallbackDecision.initial(enabled = true),
            )
            try {
                delivery = directLauncher.launch(card)
                if (delivery.decision.setupRequired) {
                    notifications.cancelLearning()
                    coordinator.markDirectSetupRequired(
                        sessionId = sessionId,
                        reason = delivery.failureReason ?: "direct_special_access_missing",
                        directLaunchAttempted = delivery.decision.directLaunchAttempted,
                    )
                    startForeground(
                        LockNotificationController.STATUS_NOTIFICATION_ID,
                        notifications.serviceNotification(setupRequired = true),
                    )
                    return@withContext
                }
                if (delivery.decision.directLaunchVisible) {
                    val directCommit = runCatching { coordinator.markDirectActivityVisible(sessionId) }
                    val accepted = directCommit.getOrDefault(false)
                    directLauncher.confirmVisibleDelivery(delivery.attemptToken, accepted)
                    if (accepted) {
                        // Remove a stale learning notification left by an older APK.
                        notifications.cancelLearning()
                        return@withContext
                    }
                    // The Activity did become visible, so this is not a launch
                    // failure/timeout and must not create a second surface.
                    // The rejected confirmation makes that Activity close.
                    return@withContext
                }

                // Defensive fail-closed branch: direct-only mode never exposes
                // lesson/quiz content through NotificationManager.
                notifications.cancelLearning()
                coordinator.markDirectSetupRequired(
                    sessionId = sessionId,
                    reason = delivery.failureReason ?: "direct_delivery_not_visible",
                    directLaunchAttempted = delivery.decision.directLaunchAttempted,
                )
                startForeground(
                    LockNotificationController.STATUS_NOTIFICATION_ID,
                    notifications.serviceNotification(setupRequired = true),
                )
            } catch (error: Throwable) {
                notifications.cancelLearning()
                coordinator.markDirectDeliveryFailed(
                    sessionId = sessionId,
                    error = "${error.javaClass.simpleName}:${error.message.orEmpty()}",
                    directLaunchAttempted = delivery.decision.directLaunchAttempted,
                    fallbackUsed = delivery.decision.fallbackUsed,
                )
                if (!notifications.statusNotificationsAvailable()) {
                    coordinator.setAppOnly("status_notification_lost_after_direct_failure")
                    stopSelf()
                } else {
                    coordinator.setAppOnly("direct_delivery_failed:${error.javaClass.simpleName}")
                    startForeground(
                        LockNotificationController.STATUS_NOTIFICATION_ID,
                        notifications.serviceNotification(setupRequired = true),
                    )
                }
            } finally {
                delayedPosts.remove(sessionId)
            }
        }
    }

    private fun cancelDelayedPosts() {
        delayedPosts.values.forEach(Job::cancel)
        delayedPosts.clear()
    }

    private fun defaultDisplayOn(): Boolean =
        getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)?.state == Display.STATE_ON

    private suspend fun awaitInteractiveDisplayOn(): Boolean {
        val startedAtNanos = System.nanoTime()
        while (true) {
            val elapsedMillis = ((System.nanoTime() - startedAtNanos) / NANOS_PER_MILLI).coerceAtLeast(0L)
            when (ScreenOnReadinessPolicy.evaluate(
                interactive = getSystemService(PowerManager::class.java).isInteractive,
                displayOn = defaultDisplayOn(),
                elapsedMillis = elapsedMillis,
            )) {
                ScreenOnReadinessDecision.READY -> return true
                ScreenOnReadinessDecision.REJECT -> return false
                ScreenOnReadinessDecision.RETRY -> delay(ScreenOnReadinessPolicy.POLL_INTERVAL_MILLIS)
            }
        }
    }

    private fun displayIsInteractiveAndOn(): Boolean =
        getSystemService(PowerManager::class.java).isInteractive && defaultDisplayOn()

    override fun onDestroy() {
        runCatching { unregisterReceiver(receiver) }
        events.close()
        delayedPosts.values.forEach(Job::cancel)
        if (!explicitlyStopped) {
            // A stale heartbeat lets the next app launch report OEM/force-stop truthfully.
        }
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private sealed interface ScreenSignal {
        data object Off : ScreenSignal
        data object On : ScreenSignal
        data object UserPresent : ScreenSignal
    }

    companion object {
        const val EXTRA_ATOM_ID = LockNotificationController.EXTRA_ATOM_ID
        const val ACTION_DISABLE = "com.example.deeplock.DISABLE_LOCK_REVIEW"
        const val ACTION_PAUSE = "com.example.deeplock.PAUSE_LOCK_REVIEW"
        const val ACTION_RESUME = "com.example.deeplock.RESUME_LOCK_REVIEW"
        const val ACTION_CARD_OPENED = "com.example.deeplock.CARD_OPENED"
        const val ACTION_CARD_SUCCESS = "com.example.deeplock.CARD_SUCCESS"
        const val ACTION_CARD_DISMISSED = "com.example.deeplock.CARD_DISMISSED"
        private const val HEARTBEAT_INTERVAL_MILLIS = 30_000L
        private const val PAUSE_MILLIS = 60 * 60 * 1000L
        private const val NANOS_PER_MILLI = 1_000_000L

        fun setEnabled(context: Context, enabled: Boolean) {
            val intent = Intent(context, LockMonitorService::class.java)
            if (enabled) ContextCompat.startForegroundService(context, intent)
            else context.stopService(intent)
        }

        fun requestPause(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, LockMonitorService::class.java).setAction(ACTION_PAUSE))
        }

        fun requestResume(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, LockMonitorService::class.java).setAction(ACTION_RESUME))
        }

        /** Kept for boot-reminder call sites; normal code uses the injected controller. */
        fun createChannels(context: Context) {
            // Keep this legacy entry point consistent with the injected path.
            LockNotificationController(context.applicationContext).createChannels()
        }
    }
}
