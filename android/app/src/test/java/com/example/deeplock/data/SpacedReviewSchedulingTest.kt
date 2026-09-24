package com.example.deeplock.data

import com.example.deeplock.data.local.LockCycleEntity
import com.example.deeplock.data.local.MistakeEntity
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class SpacedReviewSchedulingTest {
    @Test fun reviewOnlyStartsAtACleanBoundaryAndNeverInsideALessonOrBundle() {
        val clean = LockCycleEntity(mode = StudyCoordinator.READY_LESSON)

        assertThat(spacedReviewBoundaryEligible(clean)).isTrue()
        assertThat(spacedReviewBoundaryEligible(clean.copy(pendingPairId = "pair"))).isFalse()
        assertThat(spacedReviewBoundaryEligible(clean.copy(currentLessonId = "lesson"))).isFalse()
        assertThat(spacedReviewBoundaryEligible(clean.copy(currentQuestionId = "question"))).isFalse()
        assertThat(spacedReviewBoundaryEligible(clean.copy(quizRoundId = "round"))).isFalse()
        assertThat(spacedReviewBoundaryEligible(clean.copy(activeUnlockSessionId = "session"))).isFalse()
        assertThat(spacedReviewBoundaryEligible(clean.copy(normalPairRequiredAfterReview = true))).isFalse()
        assertThat(spacedReviewBoundaryEligible(spacedState())).isFalse()
    }

    @Test fun wrongReviewKeepsTheExactQuestionPinned() {
        val old = spacedState()

        val next = nextLockCycleAfterSpacedReview(old, correct = false)

        assertThat(next.mode).isEqualTo(StudyCoordinator.READY_QUIZ)
        assertThat(next.pendingPairId).isEqualTo(old.pendingPairId)
        assertThat(next.currentAtomId).isEqualTo(old.currentAtomId)
        assertThat(next.currentLessonId).isEqualTo(old.currentLessonId)
        assertThat(next.currentQuestionId).isEqualTo(old.currentQuestionId)
        assertThat(next.quizRoundId).isEqualTo(old.quizRoundId)
        assertThat(next.quizPurpose).isEqualTo(StudyCoordinator.SPACED_MISTAKE)
        assertThat(next.activeUnlockSessionId).isNull()
        assertThat(next.successfulCycles).isEqualTo(old.successfulCycles)
        assertThat(next.revision).isEqualTo(old.revision + 1)
    }

    @Test fun correctReviewClearsOnlyTheReminderAndRequiresOneNormalPair() {
        val old = spacedState()

        val next = nextLockCycleAfterSpacedReview(old, correct = true)

        assertThat(next.mode).isEqualTo(StudyCoordinator.READY_LESSON)
        assertThat(next.pendingPairId).isNull()
        assertThat(next.currentAtomId).isNull()
        assertThat(next.currentLessonId).isNull()
        assertThat(next.currentQuestionId).isNull()
        assertThat(next.quizRoundId).isNull()
        assertThat(next.quizPurpose).isEqualTo(StudyCoordinator.LEARNING_BUNDLE)
        assertThat(next.normalPairRequiredAfterReview).isTrue()
        assertThat(next.successfulCycles).isEqualTo(old.successfulCycles)
    }

    @Test fun completingTheFollowingFiveQuestionBundleReopensInterleaving() {
        val normal = spacedState().copy(
            quizPurpose = StudyCoordinator.LEARNING_BUNDLE,
            normalPairRequiredAfterReview = true,
        )

        val completed = nextLockCycleAfterQuizAnswer(normal, nextQuestionId = null)

        assertThat(completed.normalPairRequiredAfterReview).isFalse()
        assertThat(completed.successfulCycles).isEqualTo(normal.successfulCycles + 1)
        assertThat(spacedReviewBoundaryEligible(completed)).isTrue()
    }

    @Test fun disablingAndReenablingCannotEraseTheRequiredNormalPair() {
        val afterReview = nextLockCycleAfterSpacedReview(spacedState(), correct = true)

        val disabled = nextLockCycleAfterDisable(afterReview, now = 12_345L)
        val reenabledBoundary = disabled.copy(mode = StudyCoordinator.READY_LESSON)

        assertThat(disabled.normalPairRequiredAfterReview).isTrue()
        assertThat(spacedReviewBoundaryEligible(reenabledBoundary)).isFalse()
    }

    @Test fun clearingProgressDropsEveryPendingCurriculumIdentifier() {
        val old = spacedState().copy(
            mode = StudyCoordinator.PAUSED,
            normalPairRequiredAfterReview = true,
            deviceInactive = true,
            lastScreenOffAt = 99L,
        )

        val cleared = nextLockCycleAfterProgressClear(old, now = 12_345L)

        assertThat(cleared.mode).isEqualTo(StudyCoordinator.PAUSED)
        assertThat(cleared.pendingPairId).isNull()
        assertThat(cleared.currentAtomId).isNull()
        assertThat(cleared.currentLessonId).isNull()
        assertThat(cleared.currentQuestionId).isNull()
        assertThat(cleared.quizRoundId).isNull()
        assertThat(cleared.activeUnlockSessionId).isNull()
        assertThat(cleared.lastScreenOffAt).isNull()
        assertThat(cleared.normalPairRequiredAfterReview).isFalse()
        assertThat(cleared.successfulCycles).isEqualTo(0)
    }

    @Test fun mistakeScheduleUsesThreeSevenFourteenAndThirtyDaySpacing() {
        val now = 1_000L
        val initial = nextMistakeReviewSchedule(now, previous = null, correct = false, spacedReview = false)
        assertThat(initial.reviewBox).isEqualTo(0)
        assertThat(initial.dueAt).isEqualTo(now + 3L * StudyRepository.DAY_MILLIS)

        val fixedInBundle = nextMistakeReviewSchedule(
            now,
            mistake(status = "UNREVIEWED", reviewBox = 0, dueAt = initial.dueAt),
            correct = true,
            spacedReview = false,
        )
        assertThat(fixedInBundle.reviewBox).isEqualTo(0)
        assertThat(fixedInBundle.dueAt).isEqualTo(initial.dueAt)

        val seven = nextMistakeReviewSchedule(
            now,
            mistake(status = "IMPROVED", reviewBox = 0),
            correct = true,
            spacedReview = true,
        )
        val fourteen = nextMistakeReviewSchedule(
            now,
            mistake(status = "IMPROVED", reviewBox = 1),
            correct = true,
            spacedReview = true,
        )
        val thirty = nextMistakeReviewSchedule(
            now,
            mistake(status = "IMPROVED", reviewBox = 2),
            correct = true,
            spacedReview = true,
        )
        val thirtyAgain = nextMistakeReviewSchedule(
            now,
            mistake(status = "IMPROVED", reviewBox = 3),
            correct = true,
            spacedReview = true,
        )

        assertThat(seven.reviewBox).isEqualTo(1)
        assertThat(seven.dueAt).isEqualTo(now + 7L * StudyRepository.DAY_MILLIS)
        assertThat(fourteen.reviewBox).isEqualTo(2)
        assertThat(fourteen.dueAt).isEqualTo(now + 14L * StudyRepository.DAY_MILLIS)
        assertThat(thirty.reviewBox).isEqualTo(3)
        assertThat(thirty.dueAt).isEqualTo(now + 30L * StudyRepository.DAY_MILLIS)
        assertThat(thirtyAgain.reviewBox).isEqualTo(3)
        assertThat(thirtyAgain.dueAt).isEqualTo(now + 30L * StudyRepository.DAY_MILLIS)
    }

    @Test fun failedReminderResetsToThreeDaysAfterItIsRecovered() {
        val now = 9_000L
        val failed = nextMistakeReviewSchedule(
            now,
            mistake(status = "IMPROVED", reviewBox = 3),
            correct = false,
            spacedReview = true,
        )
        val recovered = nextMistakeReviewSchedule(
            now,
            mistake(status = "UNREVIEWED", reviewBox = failed.reviewBox, dueAt = failed.dueAt),
            correct = true,
            spacedReview = true,
        )

        assertThat(failed.reviewBox).isEqualTo(0)
        assertThat(failed.dueAt).isEqualTo(now + 3L * StudyRepository.DAY_MILLIS)
        assertThat(recovered.reviewBox).isEqualTo(0)
        assertThat(recovered.dueAt).isEqualTo(now + 3L * StudyRepository.DAY_MILLIS)
    }

    private fun spacedState() = LockCycleEntity(
        mode = StudyCoordinator.READY_QUIZ,
        pendingPairId = "pair",
        currentAtomId = "atom",
        currentLessonId = "lesson",
        currentQuestionId = "question",
        quizRoundId = "round",
        quizPurpose = StudyCoordinator.SPACED_MISTAKE,
        activeUnlockSessionId = "session",
        successfulCycles = 4,
        revision = 7,
    )

    private fun mistake(
        status: String,
        reviewBox: Int,
        dueAt: Long = 0,
    ) = MistakeEntity(
        questionId = "question",
        atomId = "atom",
        errorCount = 1,
        lastSelectedOptionId = "B",
        lastSeenAt = 1,
        dueAt = dueAt,
        status = status,
        reviewBox = reviewBox,
    )
}
