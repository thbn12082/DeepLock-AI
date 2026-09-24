package com.example.deeplock.lockmode

/**
 * Pure timing policy for an accepted real SCREEN_ON cycle.
 *
 * Direct delivery starts immediately. The delayed value remains only for
 * decoding legacy settings; production direct-only delivery never selects it.
 */
data class ScreenOnDeliveryTiming(
    val startDelayMillis: Long,
)

object LockDeliveryTimingPolicy {
    // One UI may need more than one frame to promote the Activity even with
    // "appear on top" access. The attempt itself remains immediate.
    const val DIRECT_FOREGROUND_TIMEOUT_MILLIS = 2_000L
    const val NOTIFICATION_BIOMETRIC_GRACE_MILLIS = 420L

    fun forScreenOn(directPrimaryEnabled: Boolean): ScreenOnDeliveryTiming =
        ScreenOnDeliveryTiming(
            startDelayMillis = if (directPrimaryEnabled) {
                0L
            } else {
                NOTIFICATION_BIOMETRIC_GRACE_MILLIS
            },
        )
}
