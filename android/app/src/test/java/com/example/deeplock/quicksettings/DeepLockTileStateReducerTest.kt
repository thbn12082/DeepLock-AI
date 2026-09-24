package com.example.deeplock.quicksettings

import com.example.deeplock.data.StudyCoordinator
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class DeepLockTileStateReducerTest {
    @Test fun missingNotificationPermissionRequiresSetup() {
        val result = DeepLockTileStateReducer.reduce(input(notificationPermission = false), NOW)

        assertThat(result).isEqualTo(DeepLockTileMode.SETUP_REQUIRED)
    }

    @Test fun invalidContentPackRequiresSetup() {
        val result = DeepLockTileStateReducer.reduce(input(contentReady = false), NOW)

        assertThat(result).isEqualTo(DeepLockTileMode.SETUP_REQUIRED)
    }

    @Test fun disabledDesiredSettingIsPausedWhenPrerequisitesAreReady() {
        val result = DeepLockTileStateReducer.reduce(
            input(
                desiredEnabled = false,
                runtimeStatus = "STOPPED",
                runtimeHeartbeatAt = 0L,
                lockMode = StudyCoordinator.DISABLED,
                stateHeartbeatAt = null,
            ),
            NOW,
        )

        assertThat(result).isEqualTo(DeepLockTileMode.PAUSED)
    }

    @Test fun everyRunningReviewModeIsActiveWithFreshRuntimeTruth() {
        listOf(
            StudyCoordinator.READY_LESSON,
            StudyCoordinator.READY_QUIZ,
            StudyCoordinator.QUIZ_RETRY,
        ).forEach { mode ->
            assertThat(DeepLockTileStateReducer.reduce(input(lockMode = mode), NOW))
                .isEqualTo(DeepLockTileMode.ACTIVE)
        }
    }

    @Test fun persistedPauseIsPausedWithoutDiscardingDesiredSetting() {
        listOf("PAUSED", "ACTIVE").forEach { runtime ->
            val result = DeepLockTileStateReducer.reduce(
                input(runtimeStatus = runtime, lockMode = StudyCoordinator.PAUSED),
                NOW,
            )

            assertThat(result).isEqualTo(DeepLockTileMode.PAUSED)
        }
    }

    @Test fun staleHeartbeatNeverClaimsActive() {
        val stale = NOW - DeepLockTileStateReducer.HEARTBEAT_FRESH_MILLIS - 1L
        val result = DeepLockTileStateReducer.reduce(
            input(runtimeHeartbeatAt = stale, stateHeartbeatAt = stale),
            NOW,
        )

        assertThat(result).isEqualTo(DeepLockTileMode.SETUP_REQUIRED)
    }

    @Test fun freshDatabaseHeartbeatCanConfirmRuntime() {
        val result = DeepLockTileStateReducer.reduce(
            input(runtimeHeartbeatAt = 0L, stateHeartbeatAt = NOW - 1L),
            NOW,
        )

        assertThat(result).isEqualTo(DeepLockTileMode.ACTIVE)
    }

    @Test fun stoppedOrAppOnlyRuntimeNeverClaimsActive() {
        listOf("STOPPED", "APP_ONLY").forEach { runtime ->
            assertThat(DeepLockTileStateReducer.reduce(input(runtimeStatus = runtime), NOW))
                .isEqualTo(DeepLockTileMode.SETUP_REQUIRED)
        }
    }

    private fun input(
        desiredEnabled: Boolean = true,
        notificationPermission: Boolean = true,
        contentReady: Boolean = true,
        runtimeStatus: String = "ACTIVE",
        runtimeHeartbeatAt: Long = NOW - 1L,
        lockMode: String? = StudyCoordinator.READY_LESSON,
        stateHeartbeatAt: Long? = NOW - 1L,
    ) = TileStateInputs(
        desiredEnabled = desiredEnabled,
        notificationPermission = notificationPermission,
        contentReady = contentReady,
        runtimeStatus = runtimeStatus,
        runtimeHeartbeatAt = runtimeHeartbeatAt,
        lockMode = lockMode,
        stateHeartbeatAt = stateHeartbeatAt,
    )

    private companion object {
        const val NOW = 1_000_000L
    }
}
