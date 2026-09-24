package com.example.deeplock.lockmode

/** Bounded decision policy for the short interactive -> display-on transition. */
internal enum class ScreenOnReadinessDecision {
    READY,
    RETRY,
    REJECT,
}

internal object ScreenOnReadinessPolicy {
    const val MAX_WAIT_MILLIS = 400L
    const val POLL_INTERVAL_MILLIS = 25L

    /**
     * Never waits for a non-interactive device, so AOD/doze remains entirely
     * system-owned. Polling is allowed only after Android has declared the
     * device interactive while the default display is still transitioning.
     */
    fun evaluate(
        interactive: Boolean,
        displayOn: Boolean,
        elapsedMillis: Long,
    ): ScreenOnReadinessDecision {
        require(elapsedMillis >= 0L) { "elapsedMillis must be non-negative" }
        return when {
            !interactive -> ScreenOnReadinessDecision.REJECT
            displayOn -> ScreenOnReadinessDecision.READY
            elapsedMillis >= MAX_WAIT_MILLIS -> ScreenOnReadinessDecision.REJECT
            else -> ScreenOnReadinessDecision.RETRY
        }
    }
}
