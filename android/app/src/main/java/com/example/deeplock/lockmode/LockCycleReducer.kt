package com.example.deeplock.lockmode

/** The persisted learning phase for the optional lock-review surface. */
enum class LockCycleMode {
    DISABLED,
    READY_LESSON,
    READY_QUIZ,
    QUIZ_RETRY, // Legacy persisted mode; new wrong answers return to a lesson first.
    PAUSED,
    APP_ONLY,
}

/** The kind of immutable payload handed to the notification controller. */
enum class PendingItemKind { LESSON, QUIZ }

/**
 * Inputs accepted by [LockCycleReducer]. Content selection remains outside the reducer; the
 * resulting stable pair/question IDs are supplied through [LockCycleEvent.PairPrepared] and
 * [LockCycleEvent.QuestionPrepared].
 */
sealed interface LockCycleEvent {
    data object Enable : LockCycleEvent
    data object Disable : LockCycleEvent

    data class PairPrepared(val pairId: String) : LockCycleEvent {
        init { require(pairId.isNotBlank()) { "pairId must not be blank" } }
    }

    data class QuestionPrepared(val pairId: String, val questionId: String) : LockCycleEvent {
        init {
            require(pairId.isNotBlank()) { "pairId must not be blank" }
            require(questionId.isNotBlank()) { "questionId must not be blank" }
        }
    }

    data object ScreenOff : LockCycleEvent
    data object ScreenOn : LockCycleEvent

    data class NotificationPosted(
        val kind: PendingItemKind,
        val pairId: String,
    ) : LockCycleEvent {
        init { require(pairId.isNotBlank()) { "pairId must not be blank" } }
    }

    data class LessonCompleted(val pairId: String, val firstQuestionId: String) : LockCycleEvent {
        init {
            require(pairId.isNotBlank()) { "pairId must not be blank" }
            require(firstQuestionId.isNotBlank()) { "firstQuestionId must not be blank" }
        }
    }

    data class QuestionPassed(val nextQuestionId: String) : LockCycleEvent {
        init { require(nextQuestionId.isNotBlank()) { "nextQuestionId must not be blank" } }
    }

    data object AnswerCorrect : LockCycleEvent
    data object AnswerWrong : LockCycleEvent
    data object SkipConfirmed : LockCycleEvent
    data object Pause : LockCycleEvent
    data object Resume : LockCycleEvent
    data object PermissionLost : LockCycleEvent
    data object PermissionRestored : LockCycleEvent
}

/**
 * Pure, persistable lock-cycle state.
 *
 * [deviceInactive] is set only by an accepted SCREEN_OFF. SCREEN_ON consumes it and grants one
 * [screenOnAccepted] notification opportunity. A successful NOTIFICATION_POSTED consumes that
 * opportunity, which makes duplicate broadcasts and duplicate post acknowledgements no-ops.
 */
data class LockCycleState(
    val mode: LockCycleMode = LockCycleMode.DISABLED,
    val pendingPairId: String? = null,
    val pendingQuestionId: String? = null,
    val deviceInactive: Boolean = false,
    val screenOnAccepted: Boolean = false,
    val revision: Long = 0L,
) {
    init {
        require(revision >= 0L) { "revision must be non-negative" }
        require(pendingQuestionId == null || pendingPairId != null) {
            "A pending question requires a pending pair"
        }
        require(!(deviceInactive && screenOnAccepted)) {
            "SCREEN_OFF and accepted SCREEN_ON cannot be active together"
        }
    }
}

object LockCycleReducer {
    private val learningModes = setOf(
        LockCycleMode.READY_LESSON,
        LockCycleMode.READY_QUIZ,
        LockCycleMode.QUIZ_RETRY,
    )

    /** Returns the same instance for a no-op; otherwise increments [LockCycleState.revision] once. */
    fun reduce(state: LockCycleState, event: LockCycleEvent): LockCycleState {
        val candidate = when (event) {
            LockCycleEvent.Enable -> enable(state)
            LockCycleEvent.Disable -> state.copy(
                mode = LockCycleMode.DISABLED,
                pendingPairId = null,
                pendingQuestionId = null,
                deviceInactive = false,
                screenOnAccepted = false,
            )

            is LockCycleEvent.PairPrepared -> preparePair(state, event)
            is LockCycleEvent.QuestionPrepared -> prepareQuestion(state, event)
            LockCycleEvent.ScreenOff -> screenOff(state)
            LockCycleEvent.ScreenOn -> screenOn(state)
            is LockCycleEvent.NotificationPosted -> notificationPosted(state, event)
            is LockCycleEvent.LessonCompleted -> lessonCompleted(state, event)
            is LockCycleEvent.QuestionPassed -> questionPassed(state, event)
            LockCycleEvent.AnswerCorrect -> answerCorrect(state)
            LockCycleEvent.AnswerWrong -> answerWrong(state)
            LockCycleEvent.SkipConfirmed -> defer(state)
            LockCycleEvent.Pause -> pause(state)
            LockCycleEvent.Resume -> resume(state)
            LockCycleEvent.PermissionLost -> permissionLost(state)
            LockCycleEvent.PermissionRestored -> permissionRestored(state)
        }

        val unchangedRevision = candidate.copy(revision = state.revision)
        return if (unchangedRevision == state) state else unchangedRevision.copy(revision = state.revision + 1L)
    }

    private fun enable(state: LockCycleState): LockCycleState = when (state.mode) {
        LockCycleMode.DISABLED -> state.copy(mode = LockCycleMode.READY_LESSON)
        else -> state
    }

    private fun preparePair(
        state: LockCycleState,
        event: LockCycleEvent.PairPrepared,
    ): LockCycleState = when {
        state.mode != LockCycleMode.READY_LESSON -> state
        state.pendingPairId == null -> state.copy(pendingPairId = event.pairId)
        else -> state // A pending pair may change only after a terminal answer/confirmed skip.
    }

    private fun prepareQuestion(
        state: LockCycleState,
        event: LockCycleEvent.QuestionPrepared,
    ): LockCycleState = when {
        state.mode !in learningModes -> state
        state.pendingPairId != event.pairId -> state
        state.pendingQuestionId != null -> state // Never replace an unanswered question.
        else -> state.copy(pendingQuestionId = event.questionId)
    }

    private fun screenOff(state: LockCycleState): LockCycleState = when {
        state.mode !in learningModes -> state
        state.deviceInactive -> state
        else -> state.copy(deviceInactive = true, screenOnAccepted = false)
    }

    private fun screenOn(state: LockCycleState): LockCycleState = when {
        state.mode !in learningModes -> state
        !state.deviceInactive -> state
        else -> state.copy(deviceInactive = false, screenOnAccepted = true)
    }

    private fun notificationPosted(
        state: LockCycleState,
        event: LockCycleEvent.NotificationPosted,
    ): LockCycleState {
        if (!state.screenOnAccepted || state.pendingPairId != event.pairId) return state

        return when (event.kind) {
            PendingItemKind.LESSON -> if (state.mode == LockCycleMode.READY_LESSON) {
                state.copy(screenOnAccepted = false)
            } else {
                state
            }

            PendingItemKind.QUIZ -> if (
                state.mode in setOf(LockCycleMode.READY_QUIZ, LockCycleMode.QUIZ_RETRY) &&
                state.pendingQuestionId != null
            ) {
                state.copy(screenOnAccepted = false)
            } else {
                state
            }
        }
    }

    private fun lessonCompleted(
        state: LockCycleState,
        event: LockCycleEvent.LessonCompleted,
    ): LockCycleState = when {
        state.mode != LockCycleMode.READY_LESSON -> state
        state.pendingPairId != event.pairId -> state
        else -> state.copy(
            mode = LockCycleMode.READY_QUIZ,
            pendingQuestionId = event.firstQuestionId,
            deviceInactive = false,
            screenOnAccepted = false,
        )
    }

    private fun questionPassed(
        state: LockCycleState,
        event: LockCycleEvent.QuestionPassed,
    ): LockCycleState = when {
        state.mode !in setOf(LockCycleMode.READY_QUIZ, LockCycleMode.QUIZ_RETRY) -> state
        state.pendingPairId == null || state.pendingQuestionId == null -> state
        else -> state.copy(
            mode = LockCycleMode.READY_QUIZ,
            pendingQuestionId = event.nextQuestionId,
            deviceInactive = false,
            screenOnAccepted = false,
        )
    }

    private fun answerCorrect(state: LockCycleState): LockCycleState = when {
        state.mode !in setOf(LockCycleMode.READY_QUIZ, LockCycleMode.QUIZ_RETRY) -> state
        state.pendingPairId == null || state.pendingQuestionId == null -> state
        else -> completePair(state)
    }

    private fun completePair(state: LockCycleState): LockCycleState = when {
        state.mode !in learningModes -> state
        state.pendingPairId == null -> state
        else -> state.copy(
            mode = LockCycleMode.READY_LESSON,
            pendingPairId = null,
            pendingQuestionId = null,
            deviceInactive = false,
            screenOnAccepted = false,
        )
    }

    private fun defer(state: LockCycleState): LockCycleState = when {
        state.mode !in learningModes -> state
        state.pendingPairId == null -> state
        else -> state.copy(deviceInactive = false, screenOnAccepted = false)
    }

    private fun answerWrong(state: LockCycleState): LockCycleState = when {
        state.mode !in setOf(LockCycleMode.READY_QUIZ, LockCycleMode.QUIZ_RETRY) -> state
        state.pendingPairId == null || state.pendingQuestionId == null -> state
        else -> state.copy(
            mode = LockCycleMode.READY_QUIZ,
            deviceInactive = false,
            screenOnAccepted = false,
        )
    }

    private fun pause(state: LockCycleState): LockCycleState = when {
        state.mode !in learningModes -> state
        else -> state.copy(
            mode = LockCycleMode.PAUSED,
            deviceInactive = false,
            screenOnAccepted = false,
        )
    }

    private fun resume(state: LockCycleState): LockCycleState = when (state.mode) {
        LockCycleMode.PAUSED -> state.copy(mode = LockCycleMode.READY_LESSON)
        else -> state
    }

    private fun permissionLost(state: LockCycleState): LockCycleState = when (state.mode) {
        LockCycleMode.DISABLED, LockCycleMode.APP_ONLY -> state
        else -> state.copy(
            mode = LockCycleMode.APP_ONLY,
            deviceInactive = false,
            screenOnAccepted = false,
        )
    }

    private fun permissionRestored(state: LockCycleState): LockCycleState = when (state.mode) {
        LockCycleMode.APP_ONLY -> state.copy(mode = LockCycleMode.READY_LESSON)
        else -> state
    }
}
