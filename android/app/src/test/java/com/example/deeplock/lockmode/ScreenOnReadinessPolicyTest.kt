package com.example.deeplock.lockmode

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class ScreenOnReadinessPolicyTest {
    @Test fun interactiveDisplayOnIsReadyImmediately() {
        assertThat(ScreenOnReadinessPolicy.evaluate(
            interactive = true,
            displayOn = true,
            elapsedMillis = 0L,
        )).isEqualTo(ScreenOnReadinessDecision.READY)
    }

    @Test fun interactiveDisplayTransitionRetriesWithinBound() {
        assertThat(ScreenOnReadinessPolicy.evaluate(
            interactive = true,
            displayOn = false,
            elapsedMillis = ScreenOnReadinessPolicy.MAX_WAIT_MILLIS - 1L,
        )).isEqualTo(ScreenOnReadinessDecision.RETRY)
    }

    @Test fun interactiveDisplayTransitionRejectsAtBound() {
        assertThat(ScreenOnReadinessPolicy.evaluate(
            interactive = true,
            displayOn = false,
            elapsedMillis = ScreenOnReadinessPolicy.MAX_WAIT_MILLIS,
        )).isEqualTo(ScreenOnReadinessDecision.REJECT)
    }

    @Test fun nonInteractiveAodIsRejectedWithoutPolling() {
        assertThat(ScreenOnReadinessPolicy.evaluate(
            interactive = false,
            displayOn = true,
            elapsedMillis = 0L,
        )).isEqualTo(ScreenOnReadinessDecision.REJECT)
    }
}
