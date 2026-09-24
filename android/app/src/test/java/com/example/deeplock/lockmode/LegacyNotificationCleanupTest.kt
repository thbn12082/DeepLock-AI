package com.example.deeplock.lockmode

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class LegacyNotificationCleanupTest {
    @Test fun inPlaceUpgradeDeletesEveryHistoricalChannelExceptCurrentStatus() {
        assertThat(LegacyNotificationCleanup.channelIdsToDelete).containsExactly(
            "deeplock_cards",
            "deeplock_learning",
            "deeplock_monitor",
        )
        assertThat(LegacyNotificationCleanup.channelIdsToDelete)
            .doesNotContain(LockNotificationController.STATUS_CHANNEL)
    }

    @Test fun anyNotificationOnHistoricalLearningChannelsIsCancelled() {
        assertThat(LegacyNotificationCleanup.shouldCancel("deeplock_cards", 41)).isTrue()
        assertThat(LegacyNotificationCleanup.shouldCancel("deeplock_learning", 99)).isTrue()
        assertThat(LegacyNotificationCleanup.shouldCancel("unknown", 2001)).isTrue()
    }

    @Test fun currentStatusAndBootReminderAreNeverClassifiedAsLearning() {
        assertThat(LegacyNotificationCleanup.shouldCancel(
            LockNotificationController.STATUS_CHANNEL,
            LockNotificationController.STATUS_NOTIFICATION_ID,
        )).isFalse()
        assertThat(LegacyNotificationCleanup.shouldCancel(
            LockNotificationController.STATUS_CHANNEL,
            LockNotificationController.BOOT_REMINDER_NOTIFICATION_ID,
        )).isFalse()
        assertThat(LegacyNotificationCleanup.shouldCancel(
            LockNotificationController.LEGACY_MONITOR_CHANNEL,
            LockNotificationController.STATUS_NOTIFICATION_ID,
        )).isFalse()
    }
}
