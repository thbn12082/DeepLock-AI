package com.example.deeplock.lockmode

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class DirectFallbackDecisionTest {
    @Test fun missingSpecialAccessRequiresSetupWithoutNotificationFallback() {
        val decision = DirectFallbackDecision.initial(
            enabled = true,
            specialAccessGranted = false,
        )

        assertThat(decision.setupRequired).isTrue()
        assertThat(decision.directLaunchAttempted).isFalse()
        assertThat(decision.directLaunchVisible).isFalse()
        assertThat(decision.fallbackUsed).isFalse()
        assertThat(decision.shouldPostNotification).isFalse()
        assertThat(decision.terminal).isTrue()
    }

    @Test fun legacyDirectDisabledStateDoesNotExposeLearningNotification() {
        val decision = DirectFallbackDecision.initial(enabled = false)

        assertThat(decision.directLaunchAttempted).isFalse()
        assertThat(decision.directLaunchVisible).isFalse()
        assertThat(decision.fallbackUsed).isFalse()
        assertThat(decision.shouldPostNotification).isFalse()
        assertThat(decision.setupRequired).isTrue()
        assertThat(decision.terminal).isTrue()
    }

    @Test fun foregroundCallbackSuppressesFallback() {
        val decision = DirectFallbackDecision.reduce(
            DirectFallbackDecision.initial(enabled = true),
            DirectDeliveryEvent.FOREGROUND_VISIBLE,
        )

        assertThat(decision.directLaunchAttempted).isTrue()
        assertThat(decision.directLaunchVisible).isTrue()
        assertThat(decision.fallbackUsed).isFalse()
        assertThat(decision.shouldPostNotification).isFalse()
        assertThat(decision.setupRequired).isFalse()
    }

    @Test fun missingCallbackRequiresSetupWithoutLearningNotification() {
        val decision = DirectFallbackDecision.reduce(
            DirectFallbackDecision.initial(enabled = true),
            DirectDeliveryEvent.FOREGROUND_TIMEOUT,
        )

        assertThat(decision.directLaunchAttempted).isTrue()
        assertThat(decision.directLaunchVisible).isFalse()
        assertThat(decision.fallbackUsed).isFalse()
        assertThat(decision.shouldPostNotification).isFalse()
        assertThat(decision.setupRequired).isTrue()
    }

    @Test fun launchExceptionRequiresSetupWithoutLearningNotification() {
        val decision = DirectFallbackDecision.reduce(
            DirectFallbackDecision.initial(enabled = true),
            DirectDeliveryEvent.LAUNCH_EXCEPTION,
        )

        assertThat(decision.fallbackUsed).isFalse()
        assertThat(decision.shouldPostNotification).isFalse()
        assertThat(decision.setupRequired).isTrue()
    }

    @Test fun lateForegroundCallbackCannotOverrideFailedDirectDecision() {
        val failed = DirectFallbackDecision.reduce(
            DirectFallbackDecision.initial(enabled = true),
            DirectDeliveryEvent.FOREGROUND_TIMEOUT,
        )

        val afterLateCallback = DirectFallbackDecision.reduce(failed, DirectDeliveryEvent.FOREGROUND_VISIBLE)

        assertThat(afterLateCallback).isEqualTo(failed)
        assertThat(afterLateCallback.fallbackUsed).isFalse()
        assertThat(afterLateCallback.shouldPostNotification).isFalse()
        assertThat(afterLateCallback.setupRequired).isTrue()
    }
}
