package com.example.deeplock.lockmode

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class LockCycleReducerTest {
    @Test fun defaultStateIsDisabled() {
        assertThat(LockCycleState()).isEqualTo(
            LockCycleState(mode = LockCycleMode.DISABLED, revision = 0L),
        )
    }

    @Test fun enableStartsAtReadyLessonAndRepeatedEnableIsNoOp() {
        val enabled = reduce(LockCycleState(), LockCycleEvent.Enable)

        assertThat(enabled.mode).isEqualTo(LockCycleMode.READY_LESSON)
        assertThat(enabled.revision).isEqualTo(1L)
        assertNoOp(enabled, LockCycleEvent.Enable)
    }

    @Test fun screenOnRequiresARealScreenOff() {
        val state = lessonState()

        assertNoOp(state, LockCycleEvent.ScreenOn)
        assertThat(state.screenOnAccepted).isFalse()
    }

    @Test fun oneScreenOffAllowsExactlyOneScreenOn() {
        val initial = lessonState()
        val off = reduce(initial, LockCycleEvent.ScreenOff)
        assertThat(off.deviceInactive).isTrue()
        assertThat(off.screenOnAccepted).isFalse()
        assertThat(off.revision).isEqualTo(initial.revision + 1L)
        assertNoOp(off, LockCycleEvent.ScreenOff)

        val on = reduce(off, LockCycleEvent.ScreenOn)
        assertThat(on.deviceInactive).isFalse()
        assertThat(on.screenOnAccepted).isTrue()
        assertThat(on.revision).isEqualTo(off.revision + 1L)
        assertNoOp(on, LockCycleEvent.ScreenOn)
    }

    @Test fun lessonPostRequiresAcceptedScreenOnAndMatchingPair() {
        val state = lessonState()
        val posted = LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR)

        assertNoOp(state, posted)

        val readyToPost = acceptedScreenOn(state)
        assertNoOp(
            readyToPost,
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, "different-pair"),
        )
    }

    @Test fun lessonVisibilityDoesNotStartAQuiz() {
        val readyToPost = acceptedScreenOn(lessonState())
        val posted = reduce(
            readyToPost,
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR),
        )

        assertThat(posted.mode).isEqualTo(LockCycleMode.READY_LESSON)
        assertThat(posted.pendingPairId).isEqualTo(PAIR)
        assertThat(posted.pendingQuestionId).isNull()
        assertThat(posted.screenOnAccepted).isFalse()
        assertThat(posted.revision).isEqualTo(readyToPost.revision + 1L)
        assertNoOp(
            posted,
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR),
        )
    }

    @Test fun explicitLessonCompletionStartsTheFirstQuiz() {
        val visible = reduce(
            acceptedScreenOn(lessonState()),
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR),
        )

        val quiz = reduce(visible, LockCycleEvent.LessonCompleted(PAIR, QUESTION))

        assertThat(quiz.mode).isEqualTo(LockCycleMode.READY_QUIZ)
        assertThat(quiz.pendingPairId).isEqualTo(PAIR)
        assertThat(quiz.pendingQuestionId).isEqualTo(QUESTION)
    }

    @Test fun quizPostConsumesCycleWithoutAdvancingReadyQuiz() {
        val readyToPost = acceptedScreenOn(quizState())
        val posted = reduce(
            readyToPost,
            LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, PAIR),
        )

        assertThat(posted.mode).isEqualTo(LockCycleMode.READY_QUIZ)
        assertThat(posted.pendingPairId).isEqualTo(PAIR)
        assertThat(posted.pendingQuestionId).isEqualTo(QUESTION)
        assertThat(posted.screenOnAccepted).isFalse()
        assertThat(posted.revision).isEqualTo(readyToPost.revision + 1L)
        assertNoOp(
            posted,
            LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, PAIR),
        )
    }

    @Test fun quizPostAlsoLeavesRetryModeUnchanged() {
        val retry = acceptedScreenOn(retryState(questionId = RETRY_QUESTION))
        val posted = reduce(
            retry,
            LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, PAIR),
        )

        assertThat(posted.mode).isEqualTo(LockCycleMode.QUIZ_RETRY)
        assertThat(posted.pendingPairId).isEqualTo(PAIR)
        assertThat(posted.pendingQuestionId).isEqualTo(RETRY_QUESTION)
        assertThat(posted.screenOnAccepted).isFalse()
    }

    @Test fun quizPostRequiresAPersistedQuestion() {
        val retryWithoutQuestion = acceptedScreenOn(retryState(questionId = null))

        assertNoOp(
            retryWithoutQuestion,
            LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, PAIR),
        )
    }

    @Test fun mismatchedNotificationKindNeverConsumesCycle() {
        val lesson = acceptedScreenOn(lessonState())
        assertNoOp(
            lesson,
            LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, PAIR),
        )

        val quiz = acceptedScreenOn(quizState())
        assertNoOp(
            quiz,
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR),
        )
    }

    @Test fun correctAnswerClearsPairAndReturnsReadyLesson() {
        val state = quizState()
        val next = reduce(state, LockCycleEvent.AnswerCorrect)

        assertCompleted(next, state.revision + 1L)
        assertNoOp(next, LockCycleEvent.AnswerCorrect)
    }

    @Test fun correctRetryClearsPairAndReturnsReadyLesson() {
        val state = retryState(questionId = RETRY_QUESTION)
        val next = reduce(state, LockCycleEvent.AnswerCorrect)

        assertCompleted(next, state.revision + 1L)
    }

    @Test fun intermediateCorrectAnswerAdvancesWithinTheSamePair() {
        val state = quizState()

        val next = reduce(state, LockCycleEvent.QuestionPassed(RETRY_QUESTION))

        assertThat(next.mode).isEqualTo(LockCycleMode.READY_QUIZ)
        assertThat(next.pendingPairId).isEqualTo(PAIR)
        assertThat(next.pendingQuestionId).isEqualTo(RETRY_QUESTION)
    }

    @Test fun answersAreIgnoredWithoutAnActiveQuizQuestion() {
        val lesson = lessonState(questionId = QUESTION)
        val retryWithoutQuestion = retryState(questionId = null)

        assertNoOp(lesson, LockCycleEvent.AnswerCorrect)
        assertNoOp(lesson, LockCycleEvent.AnswerWrong)
        assertNoOp(retryWithoutQuestion, LockCycleEvent.AnswerCorrect)
        assertNoOp(retryWithoutQuestion, LockCycleEvent.AnswerWrong)
    }

    @Test fun deferPreservesExactPairPhaseAndQuestion() {
        val states = listOf(
            lessonState(questionId = QUESTION),
            quizState(),
            retryState(questionId = RETRY_QUESTION),
        )

        states.forEach { state ->
            val deferred = reduce(state.copy(screenOnAccepted = true), LockCycleEvent.SkipConfirmed)
            assertThat(deferred.mode).isEqualTo(state.mode)
            assertThat(deferred.pendingPairId).isEqualTo(state.pendingPairId)
            assertThat(deferred.pendingQuestionId).isEqualTo(state.pendingQuestionId)
            assertThat(deferred.screenOnAccepted).isFalse()
        }
    }

    @Test fun wrongAnswerKeepsTheSameQuizRequired() {
        val state = quizState()
        val retry = reduce(state, LockCycleEvent.AnswerWrong)

        assertThat(retry.mode).isEqualTo(LockCycleMode.READY_QUIZ)
        assertThat(retry.pendingPairId).isEqualTo(PAIR)
        assertThat(retry.pendingQuestionId).isEqualTo(QUESTION)
        assertThat(retry).isSameInstanceAs(state)
    }

    @Test fun pairCannotChangeBeforeCorrectAnswerOrConfirmedSkip() {
        listOf(lessonState(), quizState(), retryState()).forEach { state ->
            assertNoOp(state, LockCycleEvent.PairPrepared("replacement-pair"))
        }
    }

    @Test fun fiftySeedRunsKeepTheSamePairUntilItsQuestionPasses() {
        repeat(50) { seed ->
            val pair = "pair-$seed"
            val question = "question-$seed-0"
            var state = LockCycleState(
                mode = LockCycleMode.READY_LESSON,
                pendingPairId = pair,
                revision = seed.toLong(),
            )

            state = acceptedScreenOn(state)
            state = reduce(state, LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, pair))
            assertThat(state.mode).isEqualTo(LockCycleMode.READY_LESSON)
            assertThat(state.pendingPairId).isEqualTo(pair)
            state = reduce(state, LockCycleEvent.LessonCompleted(pair, question))

            state = acceptedScreenOn(state)
            state = reduce(state, LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, pair))
            assertThat(state.mode).isEqualTo(LockCycleMode.READY_QUIZ)
            assertThat(state.pendingPairId).isEqualTo(pair)
            assertThat(state.pendingQuestionId).isEqualTo(question)

            state = reduce(state, LockCycleEvent.AnswerWrong)
            assertThat(state.mode).isEqualTo(LockCycleMode.READY_QUIZ)
            assertThat(state.pendingPairId).isEqualTo(pair)
            assertThat(state.pendingQuestionId).isEqualTo(question)
        }
    }

    @Test fun unansweredQuestionCannotBeReplaced() {
        listOf(lessonState(questionId = QUESTION), quizState()).forEach { state ->
            assertNoOp(
                state,
                LockCycleEvent.QuestionPrepared(PAIR, RETRY_QUESTION),
            )
        }
    }

    @Test fun questionMustBelongToCurrentPendingPair() {
        val state = lessonState(questionId = null)

        assertNoOp(
            state,
            LockCycleEvent.QuestionPrepared("different-pair", QUESTION),
        )
    }

    @Test fun pauseWorksFromEveryLearningModeAndPreservesPendingPair() {
        listOf(lessonState(), quizState(), retryState()).forEach { state ->
            val activeCycle = state.copy(deviceInactive = true)
            val paused = reduce(activeCycle, LockCycleEvent.Pause)

            assertThat(paused.mode).isEqualTo(LockCycleMode.PAUSED)
            assertThat(paused.pendingPairId).isEqualTo(PAIR)
            assertThat(paused.deviceInactive).isFalse()
            assertThat(paused.screenOnAccepted).isFalse()
            assertThat(paused.revision).isEqualTo(activeCycle.revision + 1L)
            assertNoOp(paused, LockCycleEvent.Pause)
        }
    }

    @Test fun resumeReturnsReadyLessonAndKeepsPendingPair() {
        val paused = reduce(quizState(), LockCycleEvent.Pause)
        val resumed = reduce(paused, LockCycleEvent.Resume)

        assertThat(resumed.mode).isEqualTo(LockCycleMode.READY_LESSON)
        assertThat(resumed.pendingPairId).isEqualTo(PAIR)
        assertThat(resumed.pendingQuestionId).isEqualTo(QUESTION)
        assertThat(resumed.revision).isEqualTo(paused.revision + 1L)
        assertNoOp(resumed, LockCycleEvent.Resume)
    }

    @Test fun screenEventsAreIgnoredWhileNotLearning() {
        val states = listOf(
            LockCycleState(),
            LockCycleState(mode = LockCycleMode.PAUSED),
            LockCycleState(mode = LockCycleMode.APP_ONLY),
        )

        states.forEach { state ->
            assertNoOp(state, LockCycleEvent.ScreenOff)
            assertNoOp(state, LockCycleEvent.ScreenOn)
        }
    }

    @Test fun permissionLossMovesActiveOrPausedReviewToAppOnly() {
        val states = listOf(
            lessonState(),
            quizState(),
            retryState(),
            reduce(quizState(), LockCycleEvent.Pause),
        )

        states.forEach { state ->
            val activeState = if (state.mode in learningModesForTest) {
                state.copy(deviceInactive = true)
            } else {
                state
            }
            val appOnly = reduce(activeState, LockCycleEvent.PermissionLost)
            assertThat(appOnly.mode).isEqualTo(LockCycleMode.APP_ONLY)
            assertThat(appOnly.pendingPairId).isEqualTo(PAIR)
            assertThat(appOnly.deviceInactive).isFalse()
            assertThat(appOnly.screenOnAccepted).isFalse()
            assertNoOp(appOnly, LockCycleEvent.PermissionLost)
        }
    }

    @Test fun permissionLossWhileDisabledDoesNotEnableAppOnlyMode() {
        assertNoOp(LockCycleState(), LockCycleEvent.PermissionLost)
    }

    @Test fun permissionRestoreReturnsAppOnlyToReadyLesson() {
        val appOnly = reduce(quizState(), LockCycleEvent.PermissionLost)
        val restored = reduce(appOnly, LockCycleEvent.PermissionRestored)

        assertThat(restored.mode).isEqualTo(LockCycleMode.READY_LESSON)
        assertThat(restored.pendingPairId).isEqualTo(PAIR)
        assertThat(restored.pendingQuestionId).isEqualTo(QUESTION)
        assertThat(restored.revision).isEqualTo(appOnly.revision + 1L)
        assertNoOp(restored, LockCycleEvent.PermissionRestored)
    }

    @Test fun disableAlwaysClearsStateAndRepeatedDisableIsNoOp() {
        val states = listOf(
            lessonState().copy(deviceInactive = true),
            acceptedScreenOn(quizState()),
            retryState(questionId = RETRY_QUESTION),
            reduce(quizState(), LockCycleEvent.Pause),
            reduce(quizState(), LockCycleEvent.PermissionLost),
        )

        states.forEach { state ->
            val disabled = reduce(state, LockCycleEvent.Disable)
            assertThat(disabled.mode).isEqualTo(LockCycleMode.DISABLED)
            assertThat(disabled.pendingPairId).isNull()
            assertThat(disabled.pendingQuestionId).isNull()
            assertThat(disabled.deviceInactive).isFalse()
            assertThat(disabled.screenOnAccepted).isFalse()
            assertThat(disabled.revision).isEqualTo(state.revision + 1L)
            assertNoOp(disabled, LockCycleEvent.Disable)
        }
    }

    @Test fun revisionIncrementsExactlyOnceForEachActualChange() {
        var state = LockCycleState()
        val events = listOf(
            LockCycleEvent.Enable,
            LockCycleEvent.PairPrepared(PAIR),
            LockCycleEvent.ScreenOff,
            LockCycleEvent.ScreenOn,
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR),
            LockCycleEvent.LessonCompleted(PAIR, QUESTION),
            LockCycleEvent.ScreenOff,
            LockCycleEvent.ScreenOn,
            LockCycleEvent.NotificationPosted(PendingItemKind.QUIZ, PAIR),
            LockCycleEvent.QuestionPassed(RETRY_QUESTION),
            LockCycleEvent.AnswerCorrect,
            LockCycleEvent.Disable,
        )

        events.forEachIndexed { index, event ->
            val previous = state
            state = reduce(state, event)
            assertThat(state.revision).isEqualTo(previous.revision + 1L)
            assertThat(state.revision).isEqualTo((index + 1).toLong())
        }
    }

    @Test fun invalidEventsAreIdentityNoOpsWithoutRevisionChanges() {
        val disabled = LockCycleState()
        val invalidWhileDisabled = listOf(
            LockCycleEvent.PairPrepared(PAIR),
            LockCycleEvent.QuestionPrepared(PAIR, QUESTION),
            LockCycleEvent.ScreenOff,
            LockCycleEvent.ScreenOn,
            LockCycleEvent.NotificationPosted(PendingItemKind.LESSON, PAIR),
            LockCycleEvent.LessonCompleted(PAIR, QUESTION),
            LockCycleEvent.QuestionPassed(RETRY_QUESTION),
            LockCycleEvent.AnswerCorrect,
            LockCycleEvent.AnswerWrong,
            LockCycleEvent.SkipConfirmed,
            LockCycleEvent.Pause,
            LockCycleEvent.Resume,
            LockCycleEvent.PermissionRestored,
        )

        invalidWhileDisabled.forEach { event -> assertNoOp(disabled, event) }
    }

    private fun lessonState(questionId: String? = null): LockCycleState = LockCycleState(
        mode = LockCycleMode.READY_LESSON,
        pendingPairId = PAIR,
        pendingQuestionId = questionId,
        revision = 10L,
    )

    private fun quizState(): LockCycleState = LockCycleState(
        mode = LockCycleMode.READY_QUIZ,
        pendingPairId = PAIR,
        pendingQuestionId = QUESTION,
        revision = 20L,
    )

    private fun retryState(questionId: String? = null): LockCycleState = LockCycleState(
        mode = LockCycleMode.QUIZ_RETRY,
        pendingPairId = PAIR,
        pendingQuestionId = questionId,
        revision = 30L,
    )

    private fun acceptedScreenOn(state: LockCycleState): LockCycleState = reduce(
        reduce(state, LockCycleEvent.ScreenOff),
        LockCycleEvent.ScreenOn,
    )

    private fun reduce(state: LockCycleState, event: LockCycleEvent): LockCycleState =
        LockCycleReducer.reduce(state, event)

    private fun assertNoOp(state: LockCycleState, event: LockCycleEvent) {
        val result = reduce(state, event)
        assertThat(result).isSameInstanceAs(state)
        assertThat(result.revision).isEqualTo(state.revision)
    }

    private fun assertCompleted(state: LockCycleState, expectedRevision: Long) {
        assertThat(state.mode).isEqualTo(LockCycleMode.READY_LESSON)
        assertThat(state.pendingPairId).isNull()
        assertThat(state.pendingQuestionId).isNull()
        assertThat(state.deviceInactive).isFalse()
        assertThat(state.screenOnAccepted).isFalse()
        assertThat(state.revision).isEqualTo(expectedRevision)
    }

    private companion object {
        const val PAIR = "pair-gradient"
        const val QUESTION = "question-gradient-1"
        const val RETRY_QUESTION = "question-gradient-2"
        val learningModesForTest = setOf(
            LockCycleMode.READY_LESSON,
            LockCycleMode.READY_QUIZ,
            LockCycleMode.QUIZ_RETRY,
        )
    }
}
