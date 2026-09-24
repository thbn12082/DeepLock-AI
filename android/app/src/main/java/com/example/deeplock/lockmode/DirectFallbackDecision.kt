package com.example.deeplock.lockmode

/**
 * Small, pure state machine for the primary direct-Activity delivery path.
 *
 * Learning-payload notification delivery has been removed, including for a
 * legacy `enabled=false` state. A timeout or launch exception must never
 * expose the learning payload through a notification. The cycle instead becomes
 * setup-required/app-only so the mandatory foreground-service notification can
 * report that direct delivery needs attention.
 */
data class DirectDeliveryDecision(
    val directLaunchAttempted: Boolean,
    val directLaunchVisible: Boolean,
    val fallbackUsed: Boolean,
    val shouldPostNotification: Boolean,
    val setupRequired: Boolean,
    val terminal: Boolean,
)

enum class DirectDeliveryEvent {
    FOREGROUND_VISIBLE,
    FOREGROUND_TIMEOUT,
    LAUNCH_EXCEPTION,
}

object DirectFallbackDecision {
    fun initial(
        enabled: Boolean,
        specialAccessGranted: Boolean = true,
    ): DirectDeliveryDecision = if (enabled && !specialAccessGranted) {
        DirectDeliveryDecision(
            directLaunchAttempted = false,
            directLaunchVisible = false,
            fallbackUsed = false,
            shouldPostNotification = false,
            setupRequired = true,
            terminal = true,
        )
    } else if (enabled) {
        DirectDeliveryDecision(
            directLaunchAttempted = true,
            directLaunchVisible = false,
            fallbackUsed = false,
            shouldPostNotification = false,
            setupRequired = false,
            terminal = false,
        )
    } else {
        // `enabled=false` is a legacy persisted state. Fail closed instead of
        // reviving the removed learning-notification surface.
        DirectDeliveryDecision(
            directLaunchAttempted = false,
            directLaunchVisible = false,
            fallbackUsed = false,
            shouldPostNotification = false,
            setupRequired = true,
            terminal = true,
        )
    }

    fun reduce(state: DirectDeliveryDecision, event: DirectDeliveryEvent): DirectDeliveryDecision {
        if (state.terminal) return state
        return when (event) {
            DirectDeliveryEvent.FOREGROUND_VISIBLE -> state.copy(
                directLaunchVisible = true,
                fallbackUsed = false,
                shouldPostNotification = false,
                terminal = true,
            )
            DirectDeliveryEvent.FOREGROUND_TIMEOUT,
            DirectDeliveryEvent.LAUNCH_EXCEPTION,
            -> state.copy(
                directLaunchVisible = false,
                fallbackUsed = false,
                shouldPostNotification = false,
                setupRequired = true,
                terminal = true,
            )
        }
    }
}
