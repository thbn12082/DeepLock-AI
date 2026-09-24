package com.example.deeplock.data

import com.example.deeplock.data.content.CatalogAtom
import com.example.deeplock.data.content.CatalogCategory
import com.example.deeplock.data.content.CatalogLecture
import com.example.deeplock.data.content.CatalogPack
import com.example.deeplock.data.content.ContentCatalog
import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.LockCycleEntity
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class LockPairSelectionPriorityTest {
    @Test fun dueReviewComesFirst() {
        assertThat(lockPairSelectionPriority("IN_PROGRESS", 900L, 1_000L)).isEqualTo(0)
    }

    @Test fun unseenKnowledgeComesBeforeReviewThatIsNotDue() {
        val now = 1_000L

        assertThat(lockPairSelectionPriority(null, null, now)).isEqualTo(1)
        assertThat(lockPairSelectionPriority("NEW", 0L, now)).isEqualTo(1)
        assertThat(lockPairSelectionPriority("IN_PROGRESS", 2_000L, now)).isEqualTo(2)
        assertThat(lockPairSelectionPriority("MASTERED", 2_000L, now)).isEqualTo(2)
    }

    @Test fun legacySequentialInProgressRowsRestartAtTheFirstEligibleAtom() {
        val progress = mapOf(
            "atom-1" to progress("atom-1", "IN_PROGRESS", dueAt = 2_000L),
            "atom-2" to progress("atom-2", "IN_PROGRESS", dueAt = 1L),
            "atom-3" to progress("atom-3", "IN_PROGRESS", dueAt = 1L),
            "atom-other" to progress("atom-other", "IN_PROGRESS", dueAt = 1L),
        )

        val selected = selectLockCatalogAtom(catalog(), progress, now = 1_000L)

        assertThat(selected?.atom_id).isEqualTo("atom-1")
        assertThat(selected?.pack_id).isEqualTo("pack-default")
    }

    @Test fun masteringAPrerequisiteUnlocksTheNextAtomInsteadOfReviewingItAgain() {
        val progress = mapOf(
            "atom-1" to progress("atom-1", "MASTERED", dueAt = 1L),
            "atom-2" to progress("atom-2", "IN_PROGRESS", dueAt = 2_000L),
            "atom-3" to progress("atom-3", "IN_PROGRESS", dueAt = 1L),
            "atom-other" to progress("atom-other", "IN_PROGRESS", dueAt = 1L),
        )

        val selected = selectLockCatalogAtom(catalog(), progress, now = 1_000L)

        assertThat(selected?.atom_id).isEqualTo("atom-2")
        assertThat(selected?.pack_id).isEqualTo("pack-default")
    }

    @Test fun anotherPackCannotPreemptAnUnfinishedDefaultPack() {
        val progress = mapOf(
            "atom-1" to progress("atom-1", "MASTERED"),
            "atom-2" to progress("atom-2", "IN_PROGRESS", dueAt = 2_000L),
            "atom-other" to progress("atom-other", "IN_PROGRESS", dueAt = 1L),
        )

        val selected = selectLockCatalogAtom(catalog(), progress, now = 1_000L)

        assertThat(selected?.atom_id).isEqualTo("atom-2")
    }

    @Test fun masteringTheDefaultPackMovesSelectionToTheNextCatalogPack() {
        val progress = mapOf(
            "atom-1" to progress("atom-1", "MASTERED"),
            "atom-2" to progress("atom-2", "MASTERED"),
            "atom-3" to progress("atom-3", "MASTERED"),
            "atom-other" to progress("atom-other", "IN_PROGRESS"),
        )

        val selected = selectLockCatalogAtom(catalog(), progress, now = 1_000L)

        assertThat(selected?.atom_id).isEqualTo("atom-other")
        assertThat(selected?.pack_id).isEqualTo("pack-other")
    }

    @Test fun firstFourCorrectAnswersKeepTheAtomAndAdvanceInBundleOrder() {
        QUESTIONS.dropLast(1).forEachIndexed { index, answeredQuestionId ->
            val old = quizState().copy(
                currentQuestionId = answeredQuestionId,
                revision = 8L + index,
            )
            val correctlyAnswered = QUESTIONS.take(index + 1).toSet()
            val nextQuestionId = nextRequiredQuestionId(QUESTIONS, correctlyAnswered)

            val next = nextLockCycleAfterQuizAnswer(old, nextQuestionId)

            assertThat(nextQuestionId).isEqualTo(QUESTIONS[index + 1])
            assertThat(next.mode).isEqualTo(StudyCoordinator.READY_QUIZ)
            assertThat(next.pendingPairId).isEqualTo(old.pendingPairId)
            assertThat(next.currentAtomId).isEqualTo(old.currentAtomId)
            assertThat(next.currentLessonId).isEqualTo(old.currentLessonId)
            assertThat(next.currentQuestionId).isEqualTo(QUESTIONS[index + 1])
            assertThat(next.activeUnlockSessionId).isNull()
            assertThat(next.deviceInactive).isTrue()
            assertThat(next.successfulCycles).isEqualTo(old.successfulCycles)
            assertThat(next.revision).isEqualTo(old.revision + 1)
        }
    }

    @Test fun fifthCorrectAnswerCompletesTheAtomAndIncrementsExactlyOnce() {
        val old = quizState().copy(currentQuestionId = QUESTIONS.last())
        val nextQuestionId = nextRequiredQuestionId(QUESTIONS, QUESTIONS.toSet())

        val completed = nextLockCycleAfterQuizAnswer(old, nextQuestionId)
        val duplicate = nextLockCycleAfterQuizAnswer(completed, nextQuestionId)

        assertThat(nextQuestionId).isNull()
        assertThat(completed.mode).isEqualTo(StudyCoordinator.READY_LESSON)
        assertThat(completed.pendingPairId).isNull()
        assertThat(completed.currentAtomId).isNull()
        assertThat(completed.currentLessonId).isNull()
        assertThat(completed.currentQuestionId).isNull()
        assertThat(completed.activeUnlockSessionId).isNull()
        assertThat(completed.deviceInactive).isTrue()
        assertThat(completed.successfulCycles).isEqualTo(old.successfulCycles + 1)
        assertThat(completed.revision).isEqualTo(old.revision + 1)
        assertThat(duplicate.successfulCycles).isEqualTo(completed.successfulCycles)
        assertThat(duplicate.revision).isEqualTo(completed.revision)
    }

    @Test fun wrongOrUnpassedCurrentQuestionRemainsRequired() {
        val old = quizState().copy(currentQuestionId = QUESTIONS[2])
        val correctlyAnswered = setOf(QUESTIONS[0], QUESTIONS[1])
        val nextQuestionId = nextRequiredQuestionId(QUESTIONS, correctlyAnswered)

        val next = nextLockCycleAfterQuizAnswer(old, nextQuestionId)

        assertThat(nextQuestionId).isEqualTo(QUESTIONS[2])
        assertThat(next.mode).isEqualTo(StudyCoordinator.READY_QUIZ)
        assertThat(next.pendingPairId).isEqualTo(old.pendingPairId)
        assertThat(next.currentAtomId).isEqualTo(old.currentAtomId)
        assertThat(next.currentLessonId).isEqualTo(old.currentLessonId)
        assertThat(next.currentQuestionId).isEqualTo(old.currentQuestionId)
        assertThat(next.deviceInactive).isTrue()
        assertThat(next.successfulCycles).isEqualTo(old.successfulCycles)
    }

    @Test fun nextRequiredQuestionUsesDynamicBundleOrderRatherThanSortingIds() {
        val ordered = listOf("question-z", "question-a", "question-m")

        assertThat(nextRequiredQuestionId(ordered, emptySet())).isEqualTo("question-z")
        assertThat(nextRequiredQuestionId(ordered, setOf("question-z"))).isEqualTo("question-a")
        assertThat(nextRequiredQuestionId(ordered, setOf("question-z", "question-a"))).isEqualTo("question-m")
        assertThat(nextRequiredQuestionId(ordered, ordered.toSet())).isNull()
    }

    @Test fun missingPackContentResetsAQuizToALessonWithoutLosingHistory() {
        val old = quizState().copy(
            lastEventAt = 123L,
            lastScreenOffAt = 456L,
            lastScreenOnAt = 789L,
        )

        val next = reconcilePendingContentState(old, pendingContentValid = false, now = 1_000L)

        assertThat(next.mode).isEqualTo(StudyCoordinator.READY_LESSON)
        assertThat(next.pendingPairId).isNull()
        assertThat(next.currentAtomId).isNull()
        assertThat(next.currentLessonId).isNull()
        assertThat(next.currentQuestionId).isNull()
        assertThat(next.activeUnlockSessionId).isNull()
        assertThat(next.deviceInactive).isFalse()
        assertThat(next.lastScreenOffAt).isNull()
        assertThat(next.lastScreenOnAt).isEqualTo(old.lastScreenOnAt)
        assertThat(next.lastEventAt).isEqualTo(old.lastEventAt)
        assertThat(next.successfulCycles).isEqualTo(old.successfulCycles)
        assertThat(next.revision).isEqualTo(old.revision)
    }

    @Test fun missingPackContentKeepsAnActivePauseButClearsStaleIds() {
        val old = quizState().copy(
            mode = StudyCoordinator.PAUSED,
            pausedUntil = 2_000L,
        )

        val next = reconcilePendingContentState(old, pendingContentValid = false, now = 1_000L)

        assertThat(next.mode).isEqualTo(StudyCoordinator.PAUSED)
        assertThat(next.pausedUntil).isEqualTo(2_000L)
        assertThat(next.pendingPairId).isNull()
        assertThat(next.currentQuestionId).isNull()
    }

    @Test fun expiredPauseWithMissingPackContentResumesAtALesson() {
        val old = quizState().copy(
            mode = StudyCoordinator.PAUSED,
            pausedUntil = 999L,
        )

        val next = reconcilePendingContentState(old, pendingContentValid = false, now = 1_000L)

        assertThat(next.mode).isEqualTo(StudyCoordinator.READY_LESSON)
        assertThat(next.pendingPairId).isNull()
    }

    @Test fun missingPackContentRecoversAppOnlyModeAtALesson() {
        val old = quizState().copy(mode = StudyCoordinator.APP_ONLY)

        val next = reconcilePendingContentState(old, pendingContentValid = false, now = 1_000L)

        assertThat(next.mode).isEqualTo(StudyCoordinator.READY_LESSON)
        assertThat(next.pendingPairId).isNull()
    }

    @Test fun validPendingContentIsPreservedExactly() {
        val old = quizState()

        val next = reconcilePendingContentState(old, pendingContentValid = true, now = 1_000L)

        assertThat(next).isSameInstanceAs(old)
    }

    private fun quizState() = LockCycleEntity(
        mode = StudyCoordinator.READY_QUIZ,
        pendingPairId = "pair-a",
        currentAtomId = "atom-a",
        currentLessonId = "lesson-a",
        currentQuestionId = QUESTIONS.first(),
        quizRoundId = "round-a",
        activeUnlockSessionId = "session-a",
        successfulCycles = 3,
        revision = 8L,
        deviceInactive = true,
    )

    private fun progress(
        atomId: String,
        status: String,
        dueAt: Long = 0L,
    ) = AtomProgressEntity(atomId = atomId, status = status, dueAt = dueAt)

    private fun catalog() = ContentCatalog(
        schema_version = "1.0",
        catalog_id = "catalog-test",
        title = "Test",
        language = "vi",
        default_pack_id = "pack-default",
        categories = listOf(CatalogCategory("category", "Category", order_index = 0)),
        lectures = listOf(
            CatalogLecture("lecture-default", "category", "Default", order_index = 0),
            CatalogLecture("lecture-other", "category", "Other", order_index = 1),
            CatalogLecture("lecture-later", "category", "Later", order_index = 2),
        ),
        packs = listOf(
            CatalogPack("pack-other", "lecture-other", "other.dlpack", "Other", 0, "course-other"),
            CatalogPack("pack-later", "lecture-later", "later.dlpack", "Later", 0, "course-later"),
            CatalogPack("pack-default", "lecture-default", "default.dlpack", "Default", 0, "course-default"),
        ),
        atoms = listOf(
            atom("pack-default", "atom-1", order = 0),
            atom("pack-default", "atom-2", order = 1, prerequisites = listOf("atom-1")),
            atom("pack-default", "atom-3", order = 2, prerequisites = listOf("atom-2")),
            atom("pack-other", "atom-other", order = 3),
            atom("pack-later", "atom-later", order = 4),
        ),
    )

    private fun atom(
        packId: String,
        atomId: String,
        order: Int,
        prerequisites: List<String> = emptyList(),
    ) = CatalogAtom(
        pack_id = packId,
        atom_id = atomId,
        pair_id = "pair-$atomId",
        lesson_id = "lesson-$atomId",
        module_id = "module-$packId",
        title = atomId,
        order_index = order,
        prerequisite_ids = prerequisites,
        question_ids = listOf("question-$atomId"),
    )

    private companion object {
        val QUESTIONS = listOf(
            "question-a",
            "question-b",
            "question-c",
            "question-d",
            "question-e",
        )
    }
}
