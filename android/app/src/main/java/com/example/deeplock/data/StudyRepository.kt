package com.example.deeplock.data

import androidx.room.withTransaction
import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.AttemptEntity
import com.example.deeplock.data.local.AttemptActivityRow
import com.example.deeplock.data.local.DeepLockDatabase
import com.example.deeplock.data.local.LessonActivityRow
import com.example.deeplock.data.local.LockCycleEntity
import com.example.deeplock.data.local.MistakeEntity
import com.example.deeplock.widget.WidgetSnapshotRepository
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.math.max
import kotlin.math.min
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow

data class AnswerOutcome(
    val inserted: Boolean,
    val correct: Boolean,
    val attemptUid: String,
    val accepted: Boolean = true,
)

@Singleton
class StudyRepository @Inject constructor(
    private val database: DeepLockDatabase,
    private val widgetSnapshots: WidgetSnapshotRepository,
) {
    private val dao = database.studyDao()
    val progress: Flow<List<AtomProgressEntity>> = dao.observeProgress()
    val mistakes: Flow<List<MistakeEntity>> = dao.observeMistakes()
    val attempts: Flow<List<AttemptEntity>> = dao.observeAttempts()
    val attemptActivity: Flow<List<AttemptActivityRow>> =
        dao.observeAttemptActivitySince(System.currentTimeMillis() - ANALYTICS_WINDOW_MILLIS)
    val lessonActivity: Flow<List<LessonActivityRow>> =
        dao.observeLessonActivitySince(System.currentTimeMillis() - ANALYTICS_WINDOW_MILLIS)

    suspend fun markStarted(atomId: String) {
        database.withTransaction {
            val old = dao.progress(atomId)
            if (old == null) {
                dao.putProgress(AtomProgressEntity(atomId, status = "IN_PROGRESS", mastery = .1f, lastStudiedAt = System.currentTimeMillis()))
            } else {
                dao.putProgress(old.copy(
                    status = if (old.status == "MASTERED") old.status else "IN_PROGRESS",
                    mastery = max(old.mastery, .1f),
                    lastStudiedAt = System.currentTimeMillis(),
                ))
            }
        }
        refreshWidgetSafely()
    }

    suspend fun answer(
        questionId: String,
        atomId: String,
        selectedOptionId: String,
        correctOptionId: String,
        origin: String,
        pairId: String = "",
        selectedRationale: String = "",
        preRecallMs: Long = 0L,
        preRecallSkipped: Boolean = false,
        attemptUid: String = UUID.randomUUID().toString(),
        allowMasteryCompletion: Boolean = true,
        updateAtomProgress: Boolean = true,
        canPersist: suspend () -> Boolean = { true },
        afterPersist: suspend (AnswerOutcome) -> Unit = {},
    ): AnswerOutcome {
        val outcome = database.withTransaction {
            val correct = selectedOptionId == correctOptionId
            if (dao.attemptUidCount(attemptUid) > 0) {
                return@withTransaction AnswerOutcome(inserted = false, correct = correct, attemptUid = attemptUid)
            }
            if (!canPersist()) {
                return@withTransaction AnswerOutcome(
                    inserted = false,
                    correct = correct,
                    attemptUid = attemptUid,
                    accepted = false,
                )
            }
            val now = System.currentTimeMillis()
            dao.addAttempt(AttemptEntity(
                questionId = questionId,
                atomId = atomId,
                selectedOptionId = selectedOptionId,
                correct = correct,
                origin = origin,
                createdAt = now,
                attemptUid = attemptUid,
                pairId = pairId,
                preRecallMs = preRecallMs.coerceAtLeast(0L),
                preRecallSkipped = preRecallSkipped,
            ))

            val oldProgress = dao.progress(atomId) ?: AtomProgressEntity(atomId)
            if (correct) {
                dao.mistake(questionId)?.let { old ->
                    val schedule = nextMistakeReviewSchedule(
                        now = now,
                        previous = old,
                        correct = true,
                        spacedReview = origin == ORIGIN_LOCK_SPACED_REVIEW,
                    )
                    dao.putMistake(old.copy(
                        status = "IMPROVED",
                        dueAt = schedule.dueAt,
                        improvedAt = now,
                        reviewBox = schedule.reviewBox,
                        lastReviewedAt = schedule.lastReviewedAt,
                    ))
                }
                if (updateAtomProgress) {
                    val mastery = min(1f, max(.1f, oldProgress.mastery) + .2f)
                    val mastered = oldProgress.status == "MASTERED" ||
                        (allowMasteryCompletion && mastery >= .8f)
                    val nextBox = min(5, oldProgress.leitnerBox + 1)
                    val dueDays = longArrayOf(0, 1, 3, 7, 14, 30)[nextBox]
                    dao.putProgress(oldProgress.copy(
                        status = if (mastered) "MASTERED" else "IN_PROGRESS",
                        mastery = mastery,
                        completedAt = oldProgress.completedAt ?: now.takeIf { mastered },
                        dueAt = now + dueDays * DAY_MILLIS,
                        leitnerBox = nextBox,
                        lastStudiedAt = now,
                    ))
                }
            } else {
                val old = dao.mistake(questionId)
                val schedule = nextMistakeReviewSchedule(
                    now = now,
                    previous = old,
                    correct = false,
                    spacedReview = origin == ORIGIN_LOCK_SPACED_REVIEW,
                )
                dao.putMistake(MistakeEntity(
                    questionId = questionId,
                    atomId = atomId,
                    errorCount = (old?.errorCount ?: 0) + 1,
                    lastSelectedOptionId = selectedOptionId,
                    lastSeenAt = now,
                    dueAt = schedule.dueAt,
                    pairId = pairId.ifBlank { old?.pairId.orEmpty() },
                    correctOptionId = correctOptionId,
                    selectedRationale = selectedRationale,
                    status = "UNREVIEWED",
                    userNote = old?.userNote.orEmpty(),
                    firstSeenAt = old?.firstSeenAt?.takeIf { it > 0 } ?: now,
                    improvedAt = null,
                    reviewBox = schedule.reviewBox,
                    lastReviewedAt = schedule.lastReviewedAt,
                ))
                if (updateAtomProgress) {
                    dao.putProgress(oldProgress.copy(
                        status = "IN_PROGRESS",
                        mastery = max(.1f, oldProgress.mastery - .15f),
                        dueAt = now,
                        leitnerBox = 1,
                        lastStudiedAt = now,
                    ))
                }
            }
            AnswerOutcome(inserted = true, correct = correct, attemptUid = attemptUid).also { afterPersist(it) }
        }
        if (outcome.inserted) refreshWidgetSafely()
        return outcome
    }

    suspend fun updateMistakeNote(questionId: String, note: String) {
        dao.updateMistakeNote(questionId, note.take(2_000))
        refreshWidgetSafely()
    }

    suspend fun clearAll() {
        database.withTransaction {
            dao.clearAttempts()
            dao.clearMistakes()
            dao.clearProgress()
            val lockDao = database.lockCycleDao()
            lockDao.clearEvents()
            lockDao.clearQuestionProgress()
            lockDao.get()?.let { old ->
                lockDao.put(nextLockCycleAfterProgressClear(old, System.currentTimeMillis()))
            }
        }
        refreshWidgetSafely()
    }

    private suspend fun refreshWidgetSafely() {
        try {
            widgetSnapshots.refreshAndNotify()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Throwable) {
            // Learning state is authoritative; widget rendering must never roll it back.
        }
    }

    companion object {
        const val ORIGIN_LOCK_SPACED_REVIEW = "LOCK_SPACED_REVIEW"
        const val DAY_MILLIS = 24 * 60 * 60 * 1000L
        private const val ANALYTICS_WINDOW_MILLIS = 16L * DAY_MILLIS
    }
}

/** Reset analytics and curriculum progress without leaving a stale pair that can bypass prerequisites. */
internal fun nextLockCycleAfterProgressClear(
    old: LockCycleEntity,
    now: Long,
): LockCycleEntity {
    val resetMode = when (old.mode) {
        StudyCoordinator.DISABLED, StudyCoordinator.PAUSED, StudyCoordinator.APP_ONLY -> old.mode
        else -> StudyCoordinator.READY_LESSON
    }
    return old.copy(
        mode = resetMode,
        pendingPairId = null,
        currentAtomId = null,
        currentLessonId = null,
        currentQuestionId = null,
        quizRoundId = null,
        quizPurpose = StudyCoordinator.LEARNING_BUNDLE,
        normalPairRequiredAfterReview = false,
        activeUnlockSessionId = null,
        deviceInactive = false,
        lastScreenOffAt = null,
        successfulCycles = 0,
        lastEventType = "PROGRESS_CLEARED",
        lastEventAt = now,
        revision = old.revision + 1,
    )
}

internal data class MistakeReviewSchedule(
    val reviewBox: Int,
    val dueAt: Long,
    val lastReviewedAt: Long?,
)

/** 3, 7, 14 and 30 day intervals; a failed review returns to the 3-day step. */
internal fun nextMistakeReviewSchedule(
    now: Long,
    previous: MistakeEntity?,
    correct: Boolean,
    spacedReview: Boolean,
): MistakeReviewSchedule {
    val safeNow = now.coerceAtLeast(0L)
    if (!correct) {
        return MistakeReviewSchedule(
            reviewBox = 0,
            dueAt = safeNow + 3L * StudyRepository.DAY_MILLIS,
            lastReviewedAt = if (spacedReview) safeNow else previous?.lastReviewedAt,
        )
    }
    if (!spacedReview) {
        return MistakeReviewSchedule(
            reviewBox = previous?.reviewBox?.coerceIn(0, 3) ?: 0,
            dueAt = maxOf(
                previous?.dueAt ?: 0L,
                safeNow + 3L * StudyRepository.DAY_MILLIS,
            ),
            lastReviewedAt = previous?.lastReviewedAt,
        )
    }
    val recoveredFromWrong = previous?.status == "UNREVIEWED"
    val nextBox = if (recoveredFromWrong) 0 else ((previous?.reviewBox ?: 0) + 1).coerceAtMost(3)
    val delayDays = longArrayOf(3L, 7L, 14L, 30L)[nextBox]
    return MistakeReviewSchedule(
        reviewBox = nextBox,
        dueAt = safeNow + delayDays * StudyRepository.DAY_MILLIS,
        lastReviewedAt = safeNow,
    )
}
