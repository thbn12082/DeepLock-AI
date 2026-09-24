package com.example.deeplock.lockmode

import com.example.deeplock.data.AppSettings
import com.example.deeplock.data.resolveDirectPrimaryEnabled
import com.example.deeplock.data.resolvePublicLockContent
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class LockDeliveryTimingPolicyTest {
    @Test fun freshSettingsPreferDirectDelivery() {
        assertThat(AppSettings().directExperimentalEnabled).isTrue()
    }

    @Test fun missingPersistedPreferencePrefersDirectDelivery() {
        assertThat(resolveDirectPrimaryEnabled(persistedValue = null)).isTrue()
    }

    @Test fun legacyNotificationFirstPreferenceMigratesToDirectOnly() {
        assertThat(resolveDirectPrimaryEnabled(persistedValue = false)).isTrue()
    }

    @Test fun freshSettingsShowStudyContentOnDirectBoard() {
        assertThat(AppSettings().publicLockContent).isTrue()
        assertThat(resolvePublicLockContent(persistedValue = null)).isTrue()
    }

    @Test fun legacyPrivatePlaceholderPreferenceMigratesToVisibleStudyContent() {
        assertThat(resolvePublicLockContent(persistedValue = false)).isTrue()
    }

    @Test fun directPrimaryStartsWithoutBiometricGrace() {
        val timing = LockDeliveryTimingPolicy.forScreenOn(directPrimaryEnabled = true)

        assertThat(timing.startDelayMillis).isEqualTo(0L)
        assertThat(LockDeliveryTimingPolicy.DIRECT_FOREGROUND_TIMEOUT_MILLIS).isEqualTo(2_000L)
    }

    @Test fun notificationFirstRetainsBiometricGrace() {
        val timing = LockDeliveryTimingPolicy.forScreenOn(directPrimaryEnabled = false)

        assertThat(timing.startDelayMillis)
            .isEqualTo(LockDeliveryTimingPolicy.NOTIFICATION_BIOMETRIC_GRACE_MILLIS)
        assertThat(timing.startDelayMillis).isEqualTo(420L)
    }
}
