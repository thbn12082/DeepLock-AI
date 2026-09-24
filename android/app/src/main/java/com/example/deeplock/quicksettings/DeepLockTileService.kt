package com.example.deeplock.quicksettings

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import com.example.deeplock.MainActivity
import com.example.deeplock.data.AppSettings
import com.example.deeplock.data.SettingsRepository
import com.example.deeplock.data.StudyCoordinator
import com.example.deeplock.data.content.ContentPackRepository
import com.example.deeplock.data.local.LockCycleDao
import com.example.deeplock.lockmode.LockMonitorService
import com.example.deeplock.lockmode.LockNotificationController
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

internal enum class DeepLockTileMode { ACTIVE, PAUSED, SETUP_REQUIRED }

internal data class TileStateInputs(
    val desiredEnabled: Boolean,
    val notificationPermission: Boolean,
    val contentReady: Boolean,
    val runtimeStatus: String,
    val runtimeHeartbeatAt: Long,
    val lockMode: String?,
    val stateHeartbeatAt: Long?,
)

/** Pure truth table shared by the service and JVM tests. */
internal object DeepLockTileStateReducer {
    const val HEARTBEAT_FRESH_MILLIS = 90_000L
    private val activeModes = setOf(
        StudyCoordinator.READY_LESSON,
        StudyCoordinator.READY_QUIZ,
        StudyCoordinator.QUIZ_RETRY,
    )

    fun reduce(input: TileStateInputs, now: Long): DeepLockTileMode {
        if (!input.notificationPermission || !input.contentReady) return DeepLockTileMode.SETUP_REQUIRED
        if (!input.desiredEnabled) return DeepLockTileMode.PAUSED

        val heartbeat = maxOf(input.runtimeHeartbeatAt, input.stateHeartbeatAt ?: 0L)
        val heartbeatFresh = heartbeat > 0L && heartbeat <= now + CLOCK_SKEW_TOLERANCE_MILLIS && now - heartbeat <= HEARTBEAT_FRESH_MILLIS
        if (!heartbeatFresh) return DeepLockTileMode.SETUP_REQUIRED
        if (input.lockMode == StudyCoordinator.PAUSED && input.runtimeStatus in setOf("PAUSED", "ACTIVE")) {
            return DeepLockTileMode.PAUSED
        }
        return if (input.lockMode in activeModes && input.runtimeStatus == "ACTIVE") {
            DeepLockTileMode.ACTIVE
        } else {
            DeepLockTileMode.SETUP_REQUIRED
        }
    }

    private const val CLOCK_SKEW_TOLERANCE_MILLIS = 5_000L
}

@AndroidEntryPoint
class DeepLockTileService : TileService() {
    @Inject lateinit var settings: SettingsRepository
    @Inject lateinit var content: ContentPackRepository
    @Inject lateinit var cycles: LockCycleDao
    @Inject lateinit var notifications: LockNotificationController

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    override fun onStartListening() {
        super.onStartListening()
        scope.launch { refresh() }
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")
    override fun onClick() {
        super.onClick()
        scope.launch {
            val snapshot = capture()
            when (snapshot.mode) {
                DeepLockTileMode.SETUP_REQUIRED -> openSetup()
                DeepLockTileMode.ACTIVE -> pause()
                DeepLockTileMode.PAUSED -> resume(snapshot.settings)
            }
        }
    }

    private suspend fun pause() {
        // Serialize pause with any pending notification inside the service.
        // Desired remains enabled, so the persisted pair is resumable.
        LockMonitorService.requestPause(this)
        render(DeepLockTileMode.PAUSED)
        delay(SERVICE_ACTION_GRACE_MILLIS)
        refresh()
        requestSystemRefresh()
    }

    private suspend fun resume(previous: AppSettings) {
        val prerequisites = capture()
        if (!prerequisites.inputs.notificationPermission || !prerequisites.inputs.contentReady) {
            render(DeepLockTileMode.SETUP_REQUIRED)
            openSetup()
            return
        }
        settings.setLockEnabled(true)
        if (previous.lockReviewEnabled) {
            LockMonitorService.requestResume(this)
        } else {
            LockMonitorService.setEnabled(this, true)
        }
        // Let the service validate the pack and persist state before claiming ACTIVE.
        delay(SERVICE_ACTION_GRACE_MILLIS)
        refresh()
        requestSystemRefresh()
    }

    private suspend fun refresh() = render(capture().mode)

    private suspend fun capture(now: Long = System.currentTimeMillis()): TileSnapshot {
        val appSettings = settings.settingsValue()
        val notificationPermission = notificationsGranted()
        val contentReady = runCatching { content.load() }.isSuccess
        val state = cycles.get()
        val inputs = TileStateInputs(
            desiredEnabled = appSettings.lockReviewEnabled,
            notificationPermission = notificationPermission,
            contentReady = contentReady,
            runtimeStatus = appSettings.runtimeStatus,
            runtimeHeartbeatAt = appSettings.serviceHeartbeatAt,
            lockMode = state?.mode,
            stateHeartbeatAt = state?.serviceHeartbeatAt,
        )
        return TileSnapshot(appSettings, inputs, DeepLockTileStateReducer.reduce(inputs, now))
    }

    private fun notificationsGranted(): Boolean {
        return notifications.statusNotificationsAvailable()
    }

    private fun render(mode: DeepLockTileMode) {
        qsTile?.apply {
            state = when (mode) {
                DeepLockTileMode.ACTIVE -> Tile.STATE_ACTIVE
                DeepLockTileMode.PAUSED, DeepLockTileMode.SETUP_REQUIRED ->
                    Tile.STATE_INACTIVE // Keep setup clickable; STATE_UNAVAILABLE is not interactive on some OEMs.
            }
            label = when (mode) {
                DeepLockTileMode.ACTIVE -> "DeepLock đang bật"
                DeepLockTileMode.PAUSED -> "DeepLock tạm dừng"
                DeepLockTileMode.SETUP_REQUIRED -> "Cần thiết lập DeepLock"
            }
            if (Build.VERSION.SDK_INT >= 29) {
                subtitle = when (mode) {
                    DeepLockTileMode.ACTIVE -> "ACTIVE"
                    DeepLockTileMode.PAUSED -> "PAUSED"
                    DeepLockTileMode.SETUP_REQUIRED -> "SETUP_REQUIRED"
                }
            }
            updateTile()
        }
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")
    private fun openSetup() {
        val intent = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_NEW_TASK)
            .putExtra(LockNotificationController.EXTRA_DESTINATION, LockNotificationController.DEST_SETTINGS)
        val pending = PendingIntent.getActivity(
            this,
            SETUP_REQUEST_CODE,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        if (Build.VERSION.SDK_INT >= 34) startActivityAndCollapse(pending)
        else @Suppress("DEPRECATION") startActivityAndCollapse(intent)
    }

    private fun requestSystemRefresh() =
        TileService.requestListeningState(this, ComponentName(this, DeepLockTileService::class.java))

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private data class TileSnapshot(
        val settings: AppSettings,
        val inputs: TileStateInputs,
        val mode: DeepLockTileMode,
    )

    private companion object {
        const val SETUP_REQUEST_CODE = 44
        const val SERVICE_ACTION_GRACE_MILLIS = 350L
    }
}
