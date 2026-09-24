package com.example.deeplock.app

import com.example.deeplock.lockmode.LockNotificationController
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class MainDestinationTest {
    @Test
    fun onlySettingsDeepLinkCanLeaveDashboard() {
        assertThat(resolveMainDestination(LockNotificationController.DEST_SETTINGS))
            .isEqualTo(MainDestination.SETTINGS)

        listOf(
            null,
            "",
            LockNotificationController.DEST_HOME,
            LockNotificationController.DEST_LESSON,
            LockNotificationController.DEST_QUIZ,
            "legacy-unknown",
        ).forEach { destination ->
            assertThat(resolveMainDestination(destination))
                .isEqualTo(MainDestination.DASHBOARD)
        }
    }
}
