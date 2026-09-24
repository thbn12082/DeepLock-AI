package com.example.deeplock.lockmode

import android.content.Context
import android.content.Intent
import com.example.deeplock.data.PendingLockCard
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

data class DirectLaunchResult(
    val decision: DirectDeliveryDecision,
    val attemptToken: String? = null,
    val failureKind: DirectLaunchFailureKind? = null,
    val failureReason: String? = null,
)

enum class DirectLaunchFailureKind {
    SETUP_REQUIRED,
    FOREGROUND_TIMEOUT,
    LAUNCH_EXCEPTION,
}

/** Launches the primary direct Activity path; failures require setup and never expose a learning notification. */
@Singleton
class DirectLockActivityLauncher @Inject constructor(
    @ApplicationContext private val context: Context,
    private val specialAccess: DirectLaunchSpecialAccess,
) {
    fun hasRequiredSpecialAccess(): Boolean = specialAccess.isGranted()

    suspend fun launch(card: PendingLockCard): DirectLaunchResult {
        val initial = DirectFallbackDecision.initial(
            enabled = true,
            specialAccessGranted = hasRequiredSpecialAccess(),
        )
        if (initial.setupRequired) {
            return DirectLaunchResult(
                decision = initial,
                failureKind = DirectLaunchFailureKind.SETUP_REQUIRED,
                failureReason = "system_alert_window_missing",
            )
        }
        val token = "${card.unlockSessionId}:${UUID.randomUUID()}"
        DirectActivityVisibilityRegistry.register(
            token,
            LockDeliveryTimingPolicy.DIRECT_FOREGROUND_TIMEOUT_MILLIS,
        )
        return try {
            context.startActivity(
                Intent(context, LearningCardActivity::class.java)
                    .addFlags(
                        Intent.FLAG_ACTIVITY_NEW_TASK or
                            Intent.FLAG_ACTIVITY_CLEAR_TOP or
                            Intent.FLAG_ACTIVITY_SINGLE_TOP,
                    )
                    .putExtra(EXTRA_DIRECT_ATTEMPT_TOKEN, token)
                    .putExtra(LockNotificationController.EXTRA_UNLOCK_SESSION_ID, card.unlockSessionId)
                    .putExtra(LockNotificationController.EXTRA_PACK_ID, card.packId)
                    .putExtra(EXTRA_PAIR_ID, card.pairId)
                    .putExtra(LockNotificationController.EXTRA_ATOM_ID, card.atomId)
                    .putExtra(EXTRA_LESSON_ID, card.lessonId)
                    .putExtra(LockNotificationController.EXTRA_QUESTION_ID, card.questionId)
                    .putExtra(EXTRA_ITEM_TYPE, card.itemType.name)
                    .putExtra(EXTRA_QUIZ_PURPOSE, card.quizPurpose),
            )
            if (DirectActivityVisibilityRegistry.awaitForeground(
                    token,
                    LockDeliveryTimingPolicy.DIRECT_FOREGROUND_TIMEOUT_MILLIS,
                )) {
                DirectLaunchResult(
                    DirectFallbackDecision.reduce(initial, DirectDeliveryEvent.FOREGROUND_VISIBLE),
                    attemptToken = token,
                )
            } else {
                DirectLaunchResult(
                    DirectFallbackDecision.reduce(initial, DirectDeliveryEvent.FOREGROUND_TIMEOUT),
                    failureKind = DirectLaunchFailureKind.FOREGROUND_TIMEOUT,
                    failureReason = "foreground_callback_timeout",
                )
            }
        } catch (error: Throwable) {
            DirectActivityVisibilityRegistry.cancel(token)
            DirectLaunchResult(
                DirectFallbackDecision.reduce(initial, DirectDeliveryEvent.LAUNCH_EXCEPTION),
                failureKind = DirectLaunchFailureKind.LAUNCH_EXCEPTION,
                failureReason = "${error.javaClass.simpleName}:${error.message.orEmpty()}".take(160),
            )
        }
    }

    fun confirmVisibleDelivery(token: String?, accepted: Boolean) {
        token?.let { DirectActivityVisibilityRegistry.confirmDelivery(it, accepted) }
    }

    companion object {
        const val EXTRA_DIRECT_ATTEMPT_TOKEN = "direct_attempt_token"
        const val EXTRA_PAIR_ID = "pair_id"
        const val EXTRA_LESSON_ID = "lesson_id"
        const val EXTRA_ITEM_TYPE = "item_type"
        const val EXTRA_QUIZ_PURPOSE = "quiz_purpose"
    }
}

/**
 * In-process handshake between the service and Activity. An Activity callback
 * after the monotonic deadline is rejected, so it cannot become a second card
 * after the failed direct attempt has already been rejected.
 */
internal object DirectActivityVisibilityRegistry {
    private data class Attempt(
        val deadlineNanos: Long,
        val foreground: CompletableDeferred<Boolean> = CompletableDeferred(),
        val deliveryAccepted: CompletableDeferred<Boolean> = CompletableDeferred(),
    )

    private val attempts = ConcurrentHashMap<String, Attempt>()

    fun register(token: String, timeoutMillis: Long) {
        attempts[token] = Attempt(System.nanoTime() + timeoutMillis * NANOS_PER_MILLI)
    }

    fun reportForeground(token: String): Boolean {
        val attempt = attempts[token] ?: return false
        synchronized(attempt) {
            if (System.nanoTime() > attempt.deadlineNanos) {
                attempt.foreground.complete(false)
                return false
            }
            if (attempt.foreground.isCompleted) return false
            attempt.foreground.complete(true)
            return true
        }
    }

    suspend fun awaitForeground(token: String, timeoutMillis: Long): Boolean {
        val attempt = attempts[token] ?: return false
        val remainingNanos = attempt.deadlineNanos - System.nanoTime()
        if (remainingNanos <= 0L) {
            attempts.remove(token, attempt)
            return false
        }
        val remainingMillis = (remainingNanos / NANOS_PER_MILLI).coerceIn(1L, timeoutMillis)
        val visible = withTimeoutOrNull(remainingMillis) { attempt.foreground.await() } == true
        if (!visible) attempts.remove(token, attempt)
        return visible
    }

    fun confirmDelivery(token: String, accepted: Boolean) {
        attempts[token]?.deliveryAccepted?.complete(accepted)
    }

    suspend fun awaitDeliveryAccepted(token: String): Boolean {
        val attempt = attempts[token] ?: return false
        val accepted = withTimeoutOrNull(DELIVERY_CONFIRM_TIMEOUT_MILLIS) {
            attempt.deliveryAccepted.await()
        } == true
        attempts.remove(token, attempt)
        return accepted
    }

    fun cancel(token: String) {
        attempts.remove(token)?.foreground?.cancel()
    }

    private const val NANOS_PER_MILLI = 1_000_000L
    private const val DELIVERY_CONFIRM_TIMEOUT_MILLIS = 2_000L
}
