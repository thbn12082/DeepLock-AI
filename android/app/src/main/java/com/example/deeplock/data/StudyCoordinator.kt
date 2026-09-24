package com.example.deeplock.data

import androidx.room.withTransaction
import com.example.deeplock.data.content.CatalogAtom
import com.example.deeplock.data.content.ContentCatalog
import com.example.deeplock.data.content.ContentPack
import com.example.deeplock.data.content.ContentPackRepository
import com.example.deeplock.data.content.LearningPair
import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.DeepLockDatabase
import com.example.deeplock.data.local.LockCycleEntity
import com.example.deeplock.data.local.LockQuestionProgressEntity
import com.example.deeplock.data.local.StudyEventEntity
import com.example.deeplock.data.local.UnlockSessionEntity
import java.util.Calendar
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.first

enum class LockItemType { LESSON, QUIZ }
enum class LockSurface { LOCK_SCREEN, POST_UNLOCK_HEADS_UP, TEST_CARD }

data class PendingLockCard(
    val unlockSessionId: String,
    val itemType: LockItemType,
    val packId: String,
    val pairId: String,
    val atomId: String,
    val lessonId: String,
    val questionId: String?,
    val title: String,
    val body: String,
    val surface: LockSurface,
    val quizPurpose: String? = null,
)

data class LockDiagnostics(
    val state: LockCycleEntity,
    val sessionsToday: Int,
    val notifyCallsToday: Int,
    val recentSessions: List<UnlockSessionEntity>,
    val recentEvents: List<StudyEventEntity>,
)

/**
 * Single decision/write boundary shared by Full Study, Lock Review, Tile and
 * diagnostics. Notification objects never own learning state.
 */
@Singleton
class StudyCoordinator @Inject constructor(
    private val database: DeepLockDatabase,
    private val content: ContentPackRepository,
    private val settings: SettingsRepository,
    private val study: StudyRepository,
) {
    private val lockDao get() = database.lockCycleDao()
    private val studyDao get() = database.studyDao()

    suspend fun enableLockReview(now: Long = System.currentTimeMillis()) {
        val catalog = content.catalog() // Fail before claiming the service is active.
        var runtime = "ACTIVE"
        database.withTransaction {
            val stored = lockDao.get() ?: LockCycleEntity()
            val pendingContentValid = pendingContentMatchesCatalog(stored, catalog)
            val contentReset = !pendingContentValid
            val old = reconcilePendingContentState(stored, pendingContentValid, now)
            // Older builds persisted a wrong answer as QUIZ_RETRY, which could
            // make every following lock cycle another quiz. The new sequence
            // always teaches again before asking a replacement question.
            val migrateRetryToLesson = old.mode == QUIZ_RETRY ||
                (old.mode == READY_QUIZ && old.quizRoundId == null)
            val pauseStillActive = old.mode == PAUSED && (old.pausedUntil ?: Long.MIN_VALUE) > now
            val resumable = (old.mode in ACTIVE_MODES && !migrateRetryToLesson) || pauseStillActive
            val mode = when {
                pauseStillActive -> PAUSED
                migrateRetryToLesson -> READY_LESSON
                old.mode in ACTIVE_MODES -> old.mode
                old.pendingPairId != null && old.currentQuestionId != null -> READY_QUIZ
                else -> READY_LESSON
            }
            if (contentReset || migrateRetryToLesson) {
                stored.quizRoundId?.let { lockDao.deleteQuestionProgress(it) }
            }
            runtime = if (mode == PAUSED) "PAUSED" else "ACTIVE"
            lockDao.put(old.copy(
                mode = mode,
                currentQuestionId = if (migrateRetryToLesson) null else old.currentQuestionId,
                quizRoundId = if (migrateRetryToLesson) null else old.quizRoundId,
                quizPurpose = if (migrateRetryToLesson) LEARNING_BUNDLE else old.quizPurpose,
                activeUnlockSessionId = if (migrateRetryToLesson) null else old.activeUnlockSessionId,
                pausedUntil = if (mode == PAUSED) old.pausedUntil else null,
                deviceInactive = if (resumable) old.deviceInactive else false,
                lastEventType = when {
                    contentReset -> "CONTENT_RESET_TO_LESSON"
                    migrateRetryToLesson -> "RETRY_TO_LESSON"
                    else -> "ENABLE"
                },
                lastEventAt = now,
                serviceHeartbeatAt = now,
                revision = old.revision + 1,
            ))
            event(
                when {
                    contentReset -> "pending_content_reset_to_lesson"
                    migrateRetryToLesson -> "legacy_quiz_retry_reset_to_lesson"
                    else -> "lock_service_enabled"
                },
                now,
                pairId = stored.pendingPairId,
            )
        }
        settings.setRuntimeStatus(runtime, now)
    }

    suspend fun disableLockReview(now: Long = System.currentTimeMillis()) {
        database.withTransaction {
            val old = lockDao.get() ?: LockCycleEntity()
            lockDao.put(nextLockCycleAfterDisable(old, now))
            lockDao.clearQuestionProgress()
            event("lock_service_disabled", now)
        }
        settings.setRuntimeStatus("STOPPED", now)
    }

    suspend fun markServiceStopped(reason: String, now: Long = System.currentTimeMillis()) {
        database.withTransaction {
            val old = lockDao.get() ?: LockCycleEntity()
            lockDao.put(old.copy(serviceHeartbeatAt = now, lastEventType = "SERVICE_STOPPED", lastEventAt = now))
            event("lock_service_stopped", now, detail = reason)
        }
        settings.setRuntimeStatus("STOPPED", now)
    }

    suspend fun setAppOnly(reason: String, now: Long = System.currentTimeMillis()) {
        database.withTransaction {
            val old = lockDao.get() ?: LockCycleEntity()
            lockDao.put(old.copy(
                mode = APP_ONLY,
                deviceInactive = false,
                lastEventType = "PERMISSION_LOST",
                lastEventAt = now,
                serviceHeartbeatAt = now,
                revision = old.revision + 1,
            ))
            event("lock_app_only", now, pairId = old.pendingPairId, detail = reason)
        }
        settings.setRuntimeStatus("APP_ONLY", now)
    }

    suspend fun pauseFor(durationMillis: Long, now: Long = System.currentTimeMillis()) {
        val safeDuration = durationMillis.coerceIn(0L, Long.MAX_VALUE - now.coerceAtLeast(0L))
        database.withTransaction {
            val old = lockDao.get() ?: LockCycleEntity()
            val activeSessionId = old.activeUnlockSessionId
            if (activeSessionId != null) {
                lockDao.session(activeSessionId)?.takeIf { it.notificationPostedAt == null }?.let {
                    lockDao.putSession(it.copy(outcome = "PAUSED_BEFORE_NOTIFY"))
                }
            }
            lockDao.put(old.copy(
                mode = PAUSED,
                pausedUntil = now + safeDuration,
                deviceInactive = false,
                activeUnlockSessionId = null,
                lastEventType = "PAUSE",
                lastEventAt = now,
                revision = old.revision + 1,
            ))
            event("lock_review_paused", now, pairId = old.pendingPairId)
        }
        settings.setRuntimeStatus("PAUSED", now)
    }

    suspend fun heartbeat(now: Long = System.currentTimeMillis()) {
        var runtime = "STOPPED"
        database.withTransaction {
            val old = lockDao.get() ?: LockCycleEntity()
            lockDao.put(old.copy(serviceHeartbeatAt = now))
            runtime = when (old.mode) {
                PAUSED -> "PAUSED"
                APP_ONLY -> "APP_ONLY"
                in ACTIVE_MODES -> "ACTIVE"
                else -> "STOPPED"
            }
        }
        settings.setRuntimeStatus(runtime, now)
    }

    /** Prepare and persist the exact pair/item before the hot SCREEN_ON path. */
    suspend fun onScreenOff(now: Long = System.currentTimeMillis()): Boolean {
        content.catalog()
        val appSettings = settings.settings.first()
        if (!withinActiveHours(appSettings, now)) return false
        return database.withTransaction {
            var old = lockDao.get() ?: LockCycleEntity()
            if (old.mode == PAUSED && (old.pausedUntil ?: Long.MAX_VALUE) <= now) {
                old = old.copy(mode = if (old.currentQuestionId != null) READY_QUIZ else READY_LESSON, pausedUntil = null)
            }
            if (old.mode !in ACTIVE_MODES) return@withTransaction false
            if (old.deviceInactive) return@withTransaction false
            if (old.lastEventType == "SCREEN_OFF" && now - old.lastEventAt < EVENT_DEDUPE_MILLIS) return@withTransaction false
            val prepared = prepare(old, now)
            lockDao.put(prepared.copy(
                deviceInactive = true,
                lastScreenOffAt = now,
                lastEventType = "SCREEN_OFF",
                lastEventAt = now,
                serviceHeartbeatAt = now,
                revision = prepared.revision + 1,
            ))
            event("screen_off_received", now, pairId = prepared.pendingPairId)
            true
        }
    }

    /** Creates exactly one UUID session for one consumed SCREEN_OFF token. */
    suspend fun onScreenOn(
        interactive: Boolean,
        displayOn: Boolean,
        keyguardLocked: Boolean,
        now: Long = System.currentTimeMillis(),
    ): String? {
        if (!interactive || !displayOn) return null
        return database.withTransaction {
            var old = lockDao.get() ?: return@withTransaction null
            if (old.mode !in ACTIVE_MODES || !old.deviceInactive) return@withTransaction null
            val screenOffAt = old.lastScreenOffAt ?: return@withTransaction null
            // A learner action can finish the previous card after SCREEN_OFF has
            // already prepared it. In particular, the fifth correct answer clears
            // the completed pair. Re-prepare only in that rare race so this wake
            // still shows the next lesson instead of being silently consumed.
            if (old.pendingPairId == null || old.currentAtomId == null || old.currentLessonId == null) {
                old = prepare(old, now)
            }
            val pairId = old.pendingPairId ?: return@withTransaction null
            val atomId = old.currentAtomId ?: return@withTransaction null
            val lessonId = old.currentLessonId ?: return@withTransaction null
            val itemType = if (old.mode == READY_LESSON) LockItemType.LESSON else LockItemType.QUIZ
            val questionId = if (itemType == LockItemType.QUIZ) old.currentQuestionId ?: return@withTransaction null else null
            val itemId = questionId ?: lessonId
            val id = UUID.randomUUID().toString()
            val surface = if (keyguardLocked) LockSurface.LOCK_SCREEN else LockSurface.POST_UNLOCK_HEADS_UP
            lockDao.insertSession(UnlockSessionEntity(
                unlockSessionId = id,
                screenOffAt = screenOffAt,
                screenOnAt = now,
                keyguardLocked = keyguardLocked,
                pairId = pairId,
                atomId = atomId,
                itemType = itemType.name,
                itemId = itemId,
                questionId = questionId,
                surface = surface.name,
                quizPurpose = if (itemType == LockItemType.QUIZ) old.quizPurpose else null,
            ))
            lockDao.put(old.copy(
                deviceInactive = false,
                lastScreenOnAt = now,
                activeUnlockSessionId = id,
                lastEventType = "SCREEN_ON",
                lastEventAt = now,
                serviceHeartbeatAt = now,
                revision = old.revision + 1,
            ))
            event("screen_on_received", now, id, pairId, itemId, surface.name)
            id
        }
    }

    suspend fun onUserPresent(now: Long = System.currentTimeMillis()): String? = database.withTransaction {
        val state = lockDao.get() ?: return@withTransaction null
        val id = state.activeUnlockSessionId ?: return@withTransaction null
        val session = lockDao.session(id) ?: return@withTransaction null
        if (session.userPresentAt == null) {
            val newSurface = if (session.notificationPostedAt == null) LockSurface.POST_UNLOCK_HEADS_UP.name else session.surface
            lockDao.putSession(session.copy(
                userPresentAt = now,
                surface = newSurface,
            ))
            event("user_present_received", now, id, session.pairId, session.itemId, newSurface)
        }
        if (session.notificationPostedAt == null && session.outcome == null) id else null
    }

    suspend fun pendingCard(sessionId: String): PendingLockCard? {
        val (packId, pack) = ownedPackForSession(sessionId) ?: return null
        return database.withTransaction {
            val state = lockDao.get() ?: return@withTransaction null
            if (state.mode !in ACTIVE_MODES || state.activeUnlockSessionId != sessionId) return@withTransaction null
            val session = lockDao.session(sessionId) ?: return@withTransaction null
            if (session.notificationPostedAt != null || session.outcome != null) return@withTransaction null
            val expectedType = if (state.mode == READY_LESSON) LockItemType.LESSON.name else LockItemType.QUIZ.name
            if (session.itemType != expectedType || session.pairId != state.pendingPairId) return@withTransaction null
            cardFromSession(packId, pack, session)
        }
    }

    /**
     * After a lesson is explicitly completed inside the direct lock Activity,
     * continue to the first quiz in the same visible surface. This creates a
     * fresh quiz session because the lesson session has already been finalized
     * as LESSON_COMPLETED and can no longer be treated as pending delivery.
     */
    suspend fun immediateQuizAfterCompletedLesson(
        completedLessonSessionId: String,
        now: Long = System.currentTimeMillis(),
    ): PendingLockCard? {
        val (packId, pack) = ownedPackForSession(completedLessonSessionId) ?: return null
        return database.withTransaction {
            val completed = lockDao.session(completedLessonSessionId) ?: return@withTransaction null
            if (
                completed.itemType != LockItemType.LESSON.name ||
                completed.outcome != "LESSON_COMPLETED"
            ) {
                return@withTransaction null
            }
            val state = lockDao.get() ?: return@withTransaction null
            if (
                state.mode != READY_QUIZ ||
                state.pendingPairId != completed.pairId ||
                state.currentAtomId != completed.atomId ||
                state.currentQuestionId.isNullOrBlank() ||
                state.quizRoundId.isNullOrBlank()
            ) {
                return@withTransaction null
            }
            val pair = requireNotNull(pack.pairById(completed.pairId))
            val questionId = requireNotNull(state.currentQuestionId)
            if (questionId !in pair.question_ids) return@withTransaction null
            val question = requireNotNull(pack.questionById(questionId))
            if (question.pair_id != pair.pair_id || question.atom_id != pair.atom_id) {
                return@withTransaction null
            }
            val id = UUID.randomUUID().toString()
            val session = UnlockSessionEntity(
                unlockSessionId = id,
                screenOffAt = completed.screenOffAt,
                screenOnAt = now,
                keyguardLocked = completed.keyguardLocked,
                pairId = completed.pairId,
                atomId = completed.atomId,
                itemType = LockItemType.QUIZ.name,
                itemId = questionId,
                questionId = questionId,
                surface = completed.surface,
                quizPurpose = state.quizPurpose,
            )
            lockDao.insertSession(session)
            lockDao.put(state.copy(
                deviceInactive = false,
                lastScreenOnAt = now,
                activeUnlockSessionId = id,
                lastEventType = "IMMEDIATE_QUIZ_AFTER_LESSON",
                lastEventAt = now,
                serviceHeartbeatAt = now,
                revision = state.revision + 1,
            ))
            event(
                "immediate_quiz_after_lesson",
                now,
                id,
                completed.pairId,
                questionId,
                completed.surface,
                detail = "completed_lesson_session=$completedLessonSessionId",
            )
            cardFromSession(packId, pack, session)
        }
    }

    suspend fun recoverPendingSession(): String? = database.withTransaction {
        val state = lockDao.get() ?: return@withTransaction null
        if (state.mode !in ACTIVE_MODES) return@withTransaction null
        val id = state.activeUnlockSessionId ?: return@withTransaction null
        lockDao.session(id)?.takeIf { it.notificationPostedAt == null && it.outcome == null }?.unlockSessionId
    }

    /** Finalize phase only after NotificationManager.notify() returned successfully. */
    suspend fun markNotificationPosted(
        sessionId: String,
        directLaunchAttempted: Boolean = false,
        fallbackUsed: Boolean = false,
        directFailureReason: String? = null,
        now: Long = System.currentTimeMillis(),
    ): Boolean {
        ownedPackForSession(sessionId) ?: return false
        return database.withTransaction {
            val session = lockDao.session(sessionId) ?: return@withTransaction false
            if (session.notificationPostedAt != null || session.outcome != null) return@withTransaction false
            lockDao.putSession(session.copy(notificationPostedAt = now, notifyCount = 1))
            val old = lockDao.get() ?: LockCycleEntity()
            val stateMatches = deliveryStateMatches(old, session)
            if (!stateMatches) {
                event(
                    "notification_cancelled_state_changed",
                    now, sessionId, session.pairId, session.itemId, session.surface,
                    "direct_launch_attempted=$directLaunchAttempted;direct_launch_visible=false;" +
                        "fallback_used=$fallbackUsed",
                )
                return@withTransaction false
            }
            val failure = directFailureReason?.take(120)?.let { ";direct_failure=$it" }.orEmpty()
            event(
                if (session.itemType == LockItemType.LESSON.name) "lesson_notification_posted" else "quiz_notification_posted",
                now, sessionId, session.pairId, session.itemId, session.surface,
                detail = "latency_ms=${now - session.screenOnAt};delivery=notification;" +
                    "direct_launch_attempted=$directLaunchAttempted;direct_launch_visible=false;" +
                    "fallback_used=$fallbackUsed$failure",
            )
            true
        }
    }

    /** Finalize the exact same pending session without recording a notify call. */
    suspend fun markDirectActivityVisible(sessionId: String, now: Long = System.currentTimeMillis()): Boolean {
        ownedPackForSession(sessionId) ?: return false
        return database.withTransaction {
            val session = lockDao.session(sessionId) ?: return@withTransaction false
            if (session.notificationPostedAt != null || session.outcome != null) return@withTransaction false
            val old = lockDao.get() ?: return@withTransaction false
            if (!deliveryStateMatches(old, session)) return@withTransaction false
            lockDao.putSession(session.copy(outcome = "DIRECT_VISIBLE"))
            event(
                if (session.itemType == LockItemType.LESSON.name) "lesson_direct_activity_visible" else "quiz_direct_activity_visible",
                now, sessionId, session.pairId, session.itemId, session.surface,
                detail = "latency_ms=${now - session.screenOnAt};delivery=direct_activity;" +
                    "direct_launch_attempted=true;direct_launch_visible=true;fallback_used=false",
            )
            true
        }
    }

    /** Delivery alone never means learned. Only this explicit learner action starts the quiz round. */
    suspend fun completeLesson(sessionId: String, now: Long = System.currentTimeMillis()): Boolean {
        val (_, pack) = ownedPackForSession(sessionId) ?: return false
        var completedAtomId: String? = null
        val completed = database.withTransaction {
            val session = lockDao.session(sessionId) ?: return@withTransaction false
            val old = lockDao.get() ?: return@withTransaction false
            if (session.itemType != LockItemType.LESSON.name || !deliveryStateMatches(old, session)) {
                return@withTransaction false
            }
            val roundId = UUID.randomUUID().toString()
            lockDao.put(advanceLessonToQuiz(old, session, pack, roundId, now))
            lockDao.putSession(session.copy(outcome = "LESSON_COMPLETED"))
            event(
                "lesson_completed",
                now,
                sessionId,
                session.pairId,
                session.itemId,
                session.surface,
                detail = "quiz_round_id=$roundId;question_count=${pack.pairById(session.pairId)?.question_ids?.size}",
            )
            completedAtomId = session.atomId
            true
        }
        completedAtomId?.let { study.markStarted(it) }
        return completed
    }

    /** "Để sau" consumes only this delivery; the exact lesson/question remains next in line. */
    suspend fun deferLockCard(sessionId: String, now: Long = System.currentTimeMillis()): Boolean =
        database.withTransaction {
            val session = lockDao.session(sessionId) ?: return@withTransaction false
            val old = lockDao.get() ?: return@withTransaction false
            if (!deliveryStateMatches(old, session)) return@withTransaction false
            lockDao.putSession(session.copy(outcome = "DEFERRED"))
            lockDao.put(old.copy(
                activeUnlockSessionId = null,
                lastEventType = "DEFERRED",
                lastEventAt = now,
                revision = old.revision + 1,
            ))
            event(
                if (session.itemType == LockItemType.LESSON.name) "lesson_deferred" else "quiz_deferred",
                now,
                sessionId,
                session.pairId,
                session.itemId,
                session.surface,
            )
            true
        }

    /**
     * Direct-only delivery could not become visible (missing special access,
     * OEM block, timeout, or launch error). Consume this physical cycle without
     * posting legacy notification 2001 and expose a truthful app-only state.
     */
    suspend fun markDirectSetupRequired(
        sessionId: String,
        reason: String,
        directLaunchAttempted: Boolean = false,
        now: Long = System.currentTimeMillis(),
    ): Boolean {
        val recorded = database.withTransaction {
            val session = lockDao.session(sessionId) ?: return@withTransaction false
            if (session.notificationPostedAt != null || session.outcome != null) return@withTransaction false
            val old = lockDao.get() ?: return@withTransaction false
            if (!deliveryStateMatches(old, session)) return@withTransaction false
            lockDao.putSession(session.copy(outcome = "DIRECT_SETUP_REQUIRED"))
            lockDao.put(old.copy(
                mode = APP_ONLY,
                activeUnlockSessionId = null,
                deviceInactive = false,
                lastEventType = "DIRECT_SETUP_REQUIRED",
                lastEventAt = now,
                serviceHeartbeatAt = now,
                revision = old.revision + 1,
            ))
            event(
                "direct_setup_required",
                now,
                sessionId,
                session.pairId,
                session.itemId,
                session.surface,
                "${reason.take(120)};direct_launch_attempted=$directLaunchAttempted;" +
                    "direct_launch_visible=false;fallback_used=false;notification_posted=false",
            )
            true
        }
        if (recorded) settings.setRuntimeStatus("APP_ONLY", now)
        return recorded
    }

    suspend fun markDirectDeliveryFailed(
        sessionId: String,
        error: String,
        directLaunchAttempted: Boolean = false,
        fallbackUsed: Boolean = false,
        now: Long = System.currentTimeMillis(),
    ) {
        database.withTransaction {
            val session = lockDao.session(sessionId) ?: return@withTransaction
            lockDao.putSession(session.copy(outcome = "DIRECT_FAILED"))
            event(
                "direct_delivery_failed", now, sessionId, session.pairId, session.itemId, session.surface,
                "${error.take(120)};direct_launch_attempted=$directLaunchAttempted;" +
                    "direct_launch_visible=false;fallback_used=$fallbackUsed",
            )
        }
    }

    suspend fun markNotificationOpened(sessionId: String?, now: Long = System.currentTimeMillis()) {
        database.withTransaction {
            val session = sessionId?.let { lockDao.session(it) }
            event("notification_opened", now, sessionId, session?.pairId, session?.itemId, session?.surface)
        }
    }

    suspend fun answer(
        questionId: String,
        selectedOptionId: String,
        origin: String,
        preRecallMs: Long,
        preRecallSkipped: Boolean,
        attemptUid: String,
        quizSessionId: String? = null,
        expectedLockSessionId: String? = null,
    ): AnswerOutcome {
        val pack = content.loadForQuestion(questionId)
        val question = requireNotNull(pack.questionById(questionId)) { "Unknown question" }
        val pair = requireNotNull(pack.pairById(question.pair_id)) { "Unknown pair" }
        require(question.question_id in pair.question_ids && question.atom_id == pair.atom_id) { "LearningPair invariant failed" }
        val rationale = question.options.firstOrNull { it.id == selectedOptionId }?.rationale.orEmpty()
        val persistedOrigin = if (expectedLockSessionId == null) {
            origin
        } else {
            database.withTransaction {
                val state = lockDao.get()
                val session = lockDao.session(expectedLockSessionId)
                if (
                    directAnswerStateMatches(state, session, pair.pair_id, questionId) &&
                    state?.quizPurpose == SPACED_MISTAKE
                ) {
                    StudyRepository.ORIGIN_LOCK_SPACED_REVIEW
                } else {
                    origin
                }
            }
        }
        return study.answer(
            questionId = questionId,
            atomId = question.atom_id,
            selectedOptionId = selectedOptionId,
            correctOptionId = question.correct_option_id,
            origin = persistedOrigin,
            pairId = pair.pair_id,
            selectedRationale = rationale,
            preRecallMs = preRecallMs,
            preRecallSkipped = preRecallSkipped,
            attemptUid = attemptUid,
            allowMasteryCompletion = expectedLockSessionId == null,
            updateAtomProgress = persistedOrigin != StudyRepository.ORIGIN_LOCK_SPACED_REVIEW,
            canPersist = {
                if (expectedLockSessionId == null) {
                    true
                } else {
                    val state = lockDao.get()
                    val session = lockDao.session(expectedLockSessionId)
                    origin == "LOCK_REVIEW" &&
                        directAnswerStateMatches(state, session, pair.pair_id, questionId)
                }
            },
            afterPersist = { outcome ->
            val old = lockDao.get() ?: LockCycleEntity()
            val now = System.currentTimeMillis()
            if (outcome.inserted && quizSessionId != null) {
                database.quizSessionDao().get(quizSessionId)?.let { session ->
                    database.quizSessionDao().put(session.copy(
                        phase = "SUBMITTED",
                        selectedOptionId = selectedOptionId,
                        submittedAttemptUid = attemptUid,
                        updatedAt = now,
                    ))
                }
            }
            val directSession = expectedLockSessionId?.let { lockDao.session(it) }
            var nextQuestionId: String? = old.currentQuestionId
            var passedCount: Int? = null
            var spacedReviewAnswer = false
            if (outcome.inserted && directAnswerStateMatches(old, directSession, pair.pair_id, questionId)) {
                spacedReviewAnswer = old.quizPurpose == SPACED_MISTAKE
                if (spacedReviewAnswer) {
                    nextQuestionId = questionId.takeUnless { outcome.correct }
                    val next = nextLockCycleAfterSpacedReview(old, outcome.correct)
                    lockDao.put(next.copy(
                        lastEventType = if (outcome.correct) {
                            "SPACED_REVIEW_COMPLETED"
                        } else {
                            "SPACED_REVIEW_WRONG"
                        },
                        lastEventAt = now,
                    ))
                    requireNotNull(directSession).let { session ->
                        lockDao.putSession(session.copy(
                            outcome = if (outcome.correct) "SPACED_CORRECT" else "SPACED_WRONG",
                        ))
                    }
                } else {
                    val roundId = requireNotNull(old.quizRoundId)
                    val previous = lockDao.questionProgress(roundId, questionId)
                    lockDao.putQuestionProgress(LockQuestionProgressEntity(
                        quizRoundId = roundId,
                        questionId = questionId,
                        pairId = pair.pair_id,
                        atomId = pair.atom_id,
                        correct = previous?.correct == true || outcome.correct,
                        wrongCount = (previous?.wrongCount ?: 0) + if (outcome.correct) 0 else 1,
                        lastAttemptAt = now,
                        correctAt = if (outcome.correct) now else previous?.correctAt,
                    ))
                    val correctlyAnswered = lockDao.correctQuestionIds(roundId).toSet()
                    passedCount = correctlyAnswered.size
                    nextQuestionId = nextRequiredQuestionId(pair.question_ids, correctlyAnswered)
                    val next = nextLockCycleAfterQuizAnswer(old, nextQuestionId)
                    if (nextQuestionId == null) {
                        studyDao.progress(question.atom_id)?.let { progress ->
                            studyDao.putProgress(progress.copy(
                                status = "MASTERED",
                                mastery = maxOf(.8f, progress.mastery),
                                completedAt = progress.completedAt ?: now,
                            ))
                        }
                    }
                    lockDao.put(next.copy(
                        lastEventType = if (nextQuestionId == null) {
                            "QUIZ_BUNDLE_COMPLETED"
                        } else if (outcome.correct) {
                            "QUESTION_PASSED"
                        } else {
                            "ANSWER_WRONG"
                        },
                        lastEventAt = now,
                    ))
                    requireNotNull(directSession).let { session ->
                        lockDao.putSession(session.copy(outcome = if (outcome.correct) "CORRECT" else "WRONG"))
                    }
                    if (nextQuestionId == null) lockDao.deleteQuestionProgress(roundId)
                }
            }
            event(
                if (outcome.correct) "quiz_answered_correct" else "quiz_answered_wrong",
                now,
                expectedLockSessionId ?: old.activeUnlockSessionId,
                pair.pair_id,
                questionId,
                detail = when {
                    spacedReviewAnswer ->
                        "surface=$persistedOrigin;spaced_review=true;correct=${outcome.correct};" +
                            "next_question=${nextQuestionId ?: "none"}"
                    passedCount == null -> "surface=$persistedOrigin;lock_bundle_unchanged=true"
                    else -> "passed=$passedCount/${pair.question_ids.size};" +
                        "next_question=${nextQuestionId ?: "none"};bundle_complete=${nextQuestionId == null}"
                },
            )
        },
        )
    }

    suspend fun diagnostics(now: Long = System.currentTimeMillis()): LockDiagnostics = database.withTransaction {
        val start = localDayStart(now)
        LockDiagnostics(
            state = lockDao.get() ?: LockCycleEntity(),
            sessionsToday = lockDao.sessionCountSince(start),
            notifyCallsToday = lockDao.notifyCountSince(start),
            recentSessions = lockDao.recentSessions(),
            recentEvents = lockDao.recentEvents(),
        )
    }

    private data class OwnedPair(val packId: String, val pack: ContentPack, val pair: LearningPair)
    private data class OwnedSpacedReview(val owned: OwnedPair, val questionId: String)

    private suspend fun prepare(old: LockCycleEntity, now: Long): LockCycleEntity {
        val existing = old.pendingPairId?.let { pairId ->
            content.packIdForPair(pairId)?.let { packId ->
                val pack = content.load(packId)
                pack.pairById(pairId)?.let { OwnedPair(packId, pack, it) }
            }
        }
        if (existing == null && spacedReviewBoundaryEligible(old)) {
            selectDueSpacedReview(now)?.let { review ->
                val pair = review.owned.pair
                val lesson = requireNotNull(review.owned.pack.lessonById(pair.micro_lesson_id))
                require(lesson.atom_id == pair.atom_id)
                return old.copy(
                    mode = READY_QUIZ,
                    pendingPairId = pair.pair_id,
                    currentAtomId = pair.atom_id,
                    currentLessonId = lesson.lesson_id,
                    currentQuestionId = review.questionId,
                    quizRoundId = UUID.randomUUID().toString(),
                    quizPurpose = SPACED_MISTAKE,
                )
            }
        }
        val owned = existing ?: selectPair(now)
        val pack = owned.pack
        val pair = owned.pair
        val lesson = requireNotNull(pack.lessonById(pair.micro_lesson_id))
        require(lesson.atom_id == pair.atom_id)
        val questionId = when {
            old.mode == READY_LESSON -> null
            old.quizPurpose == SPACED_MISTAKE -> {
                requireNotNull(old.currentQuestionId?.let(pack::questionById)) {
                    "Spaced review requires its persisted question"
                }.also { question ->
                    require(question.pair_id == pair.pair_id) { "Spaced review question changed pair" }
                }.question_id
            }
            else -> {
            val roundId = requireNotNull(old.quizRoundId) { "Quiz state requires a persisted round" }
            val existingQuestion = old.currentQuestionId?.let(pack::questionById)
                ?.takeIf { it.pair_id == pair.pair_id }
            existingQuestion?.question_id ?: requireNotNull(nextRequiredQuestionId(
                pair.question_ids,
                lockDao.correctQuestionIds(roundId).toSet(),
            )) { "Completed quiz round was not finalized" }
            }
        }
        return old.copy(
            pendingPairId = pair.pair_id,
            currentAtomId = pair.atom_id,
            currentLessonId = lesson.lesson_id,
            currentQuestionId = questionId,
            quizRoundId = if (old.mode == READY_LESSON) null else old.quizRoundId,
            quizPurpose = if (old.mode == READY_LESSON) LEARNING_BUNDLE else old.quizPurpose,
        )
    }

    private fun pendingContentMatchesCatalog(old: LockCycleEntity, catalog: ContentCatalog): Boolean {
        val hasPendingIds = old.pendingPairId != null ||
            old.currentAtomId != null ||
            old.currentLessonId != null ||
            old.currentQuestionId != null ||
            old.quizRoundId != null
        if (!hasPendingIds) {
            return old.mode !in setOf(READY_QUIZ, QUIZ_RETRY)
        }
        val atom = old.pendingPairId?.let(catalog::atomByPair) ?: return false
        if (
            old.currentAtomId != atom.atom_id ||
            old.currentLessonId != atom.lesson_id
        ) {
            return false
        }
        val questionId = old.currentQuestionId
        val questionValid = questionId != null && questionId in atom.question_ids
        return when (old.mode) {
            READY_LESSON -> questionId == null &&
                old.quizRoundId == null &&
                old.quizPurpose == LEARNING_BUNDLE
            READY_QUIZ, QUIZ_RETRY -> questionValid &&
                !old.quizRoundId.isNullOrBlank() &&
                old.quizPurpose in QUIZ_PURPOSES
            else -> (questionId == null && old.quizRoundId == null) ||
                (questionValid && !old.quizRoundId.isNullOrBlank() && old.quizPurpose in QUIZ_PURPOSES)
        }
    }

    private fun deliveryStateMatches(old: LockCycleEntity, session: UnlockSessionEntity): Boolean =
        old.activeUnlockSessionId == session.unlockSessionId &&
            old.pendingPairId == session.pairId &&
            when (session.itemType) {
                LockItemType.LESSON.name -> old.mode == READY_LESSON && session.quizPurpose == null
                LockItemType.QUIZ.name ->
                    old.mode in setOf(READY_QUIZ, QUIZ_RETRY) &&
                        old.quizRoundId != null &&
                        session.quizPurpose == old.quizPurpose &&
                        old.currentQuestionId == session.questionId
                else -> false
            }

    private fun directAnswerStateMatches(
        state: LockCycleEntity?,
        session: UnlockSessionEntity?,
        pairId: String,
        questionId: String,
    ): Boolean = state != null &&
        session != null &&
        session.itemType == LockItemType.QUIZ.name &&
        session.pairId == pairId &&
        session.questionId == questionId &&
        session.quizPurpose == state.quizPurpose &&
        state.activeUnlockSessionId == session.unlockSessionId &&
        state.pendingPairId == pairId &&
        state.mode in setOf(READY_QUIZ, QUIZ_RETRY) &&
        state.quizRoundId != null &&
        state.currentQuestionId == questionId

    private fun advanceLessonToQuiz(
        old: LockCycleEntity,
        session: UnlockSessionEntity,
        pack: ContentPack,
        roundId: String,
        now: Long,
    ): LockCycleEntity {
        val pair = requireNotNull(pack.pairById(session.pairId))
        val questionId = requireNotNull(pair.question_ids.firstOrNull()) { "Learning pair has no quiz" }
        require(pack.questionById(questionId)?.pair_id == pair.pair_id)
        return old.copy(
            mode = READY_QUIZ,
            currentQuestionId = questionId,
            quizRoundId = roundId,
            quizPurpose = LEARNING_BUNDLE,
            activeUnlockSessionId = null,
            lastEventType = "LESSON_COMPLETED",
            lastEventAt = now,
            revision = old.revision + 1,
        )
    }

    private suspend fun selectDueSpacedReview(now: Long): OwnedSpacedReview? {
        for (mistake in studyDao.dueMasteredMistakes(now)) {
            val packId = content.packIdForQuestion(mistake.questionId) ?: continue
            val pack = content.load(packId)
            val question = pack.questionById(mistake.questionId) ?: continue
            val pair = pack.pairById(question.pair_id) ?: continue
            if (
                question.atom_id != mistake.atomId ||
                pair.atom_id != mistake.atomId ||
                question.question_id !in pair.question_ids
            ) {
                continue
            }
            return OwnedSpacedReview(OwnedPair(packId, pack, pair), question.question_id)
        }
        return null
    }

    private suspend fun selectPair(now: Long): OwnedPair {
        val catalog = content.catalog()
        val progress = studyDao.progressNow().associateBy { it.atomId }
        val summary = requireNotNull(selectLockCatalogAtom(catalog, progress, now)) {
            "The first unfinished content pack has no prerequisite-eligible learning pair"
        }
        val pack = content.load(summary.pack_id)
        val pair = requireNotNull(pack.pairById(summary.pair_id)) { "Catalog pair is missing from pack" }
        require(pair.atom_id == summary.atom_id && pair.micro_lesson_id == summary.lesson_id)
        return OwnedPair(summary.pack_id, pack, pair)
    }

    private suspend fun ownedPackForSession(sessionId: String): Pair<String, ContentPack>? {
        val pairId = lockDao.session(sessionId)?.pairId ?: return null
        val packId = content.packIdForPair(pairId) ?: return null
        return packId to content.load(packId)
    }

    private fun cardFromSession(packId: String, pack: ContentPack, session: UnlockSessionEntity): PendingLockCard {
        val lesson = requireNotNull(pack.lessonById(pack.pairById(session.pairId)?.micro_lesson_id.orEmpty()))
        return if (session.itemType == LockItemType.LESSON.name) {
            PendingLockCard(
                unlockSessionId = session.unlockSessionId,
                itemType = LockItemType.LESSON,
                packId = packId,
                pairId = session.pairId,
                atomId = session.atomId,
                lessonId = lesson.lesson_id,
                questionId = null,
                title = lesson.title,
                body = "${lesson.hook}\n${lesson.intuition}",
                surface = LockSurface.valueOf(session.surface),
                quizPurpose = null,
            )
        } else {
            val question = requireNotNull(session.questionId?.let(pack::questionById))
            require(question.pair_id == session.pairId)
            PendingLockCard(
                unlockSessionId = session.unlockSessionId,
                itemType = LockItemType.QUIZ,
                packId = packId,
                pairId = session.pairId,
                atomId = session.atomId,
                lessonId = lesson.lesson_id,
                questionId = question.question_id,
                title = if (session.quizPurpose == SPACED_MISTAKE) {
                    "Ôn lại câu từng sai"
                } else {
                    "Tự nhớ rồi trả lời"
                },
                body = question.stem,
                surface = LockSurface.valueOf(session.surface),
                quizPurpose = session.quizPurpose,
            )
        }
    }

    private suspend fun event(
        type: String,
        at: Long,
        sessionId: String? = null,
        pairId: String? = null,
        itemId: String? = null,
        surface: String? = null,
        detail: String? = null,
    ) = lockDao.addEvent(StudyEventEntity(
        type = type,
        createdAt = at,
        unlockSessionId = sessionId,
        pairId = pairId,
        itemId = itemId,
        surface = surface,
        detail = detail,
    ))

    private fun withinActiveHours(settings: AppSettings, now: Long): Boolean {
        val hour = Calendar.getInstance().apply { timeInMillis = now }.get(Calendar.HOUR_OF_DAY)
        val start = settings.activeHourStart
        val end = settings.activeHourEnd
        return if (start < end) hour in start until end else hour >= start || hour < end
    }

    private fun localDayStart(now: Long): Long = Calendar.getInstance().apply {
        timeInMillis = now
        set(Calendar.HOUR_OF_DAY, 0)
        set(Calendar.MINUTE, 0)
        set(Calendar.SECOND, 0)
        set(Calendar.MILLISECOND, 0)
    }.timeInMillis

    companion object {
        const val DISABLED = "DISABLED"
        const val READY_LESSON = "READY_LESSON"
        const val READY_QUIZ = "READY_QUIZ"
        const val QUIZ_RETRY = "QUIZ_RETRY"
        const val PAUSED = "PAUSED"
        const val APP_ONLY = "APP_ONLY"
        const val LEARNING_BUNDLE = "LEARNING_BUNDLE"
        const val SPACED_MISTAKE = "SPACED_MISTAKE"
        val QUIZ_PURPOSES = setOf(LEARNING_BUNDLE, SPACED_MISTAKE)
        val ACTIVE_MODES = setOf(READY_LESSON, READY_QUIZ, QUIZ_RETRY)
        const val EVENT_DEDUPE_MILLIS = 2_000L
    }
}

/** Due review first, then unseen knowledge, then material that is not due yet. */
internal fun lockPairSelectionPriority(status: String?, dueAt: Long?, now: Long): Int = when {
    dueAt != null && dueAt in 1..now -> 0
    status == null || status == "NEW" -> 1
    else -> 2
}

/**
 * Finish one pack before entering the next one. While a pack is unfinished,
 * mastered atoms are not scheduled for review and an atom is unlocked only
 * after every declared prerequisite is fully mastered. This also repairs the
 * effective ordering of legacy rows that marked several sequential atoms as
 * IN_PROGRESS at the same time.
 */
internal fun selectLockCatalogAtom(
    catalog: ContentCatalog,
    progress: Map<String, AtomProgressEntity>,
    now: Long,
): CatalogAtom? {
    val atomsByPack = catalog.atoms.groupBy(CatalogAtom::pack_id)
    val packOrder = buildList {
        add(catalog.default_pack_id)
        catalog.packs.forEach { descriptor ->
            if (descriptor.pack_id != catalog.default_pack_id) add(descriptor.pack_id)
        }
    }
    val unfinishedPackId = packOrder.firstOrNull { packId ->
        atomsByPack[packId].orEmpty().any { atom ->
            progress[atom.atom_id]?.status != "MASTERED"
        }
    }
    if (unfinishedPackId == null) {
        // The whole catalog is mastered: return to normal due-review ordering.
        return catalog.atoms.minWithOrNull(lockAtomComparator(progress, now))
    }
    return atomsByPack[unfinishedPackId].orEmpty()
        .asSequence()
        .filter { atom -> progress[atom.atom_id]?.status != "MASTERED" }
        .filter { atom ->
            atom.prerequisite_ids.all { prerequisiteId ->
                progress[prerequisiteId]?.status == "MASTERED"
            }
        }
        .minWithOrNull(lockAtomComparator(progress, now))
}

private fun lockAtomComparator(
    progress: Map<String, AtomProgressEntity>,
    now: Long,
) = compareBy<CatalogAtom>(
    { atom ->
        progress[atom.atom_id]?.let { item ->
            lockPairSelectionPriority(item.status, item.dueAt, now)
        } ?: lockPairSelectionPriority(null, null, now)
    },
    { atom -> progress[atom.atom_id]?.dueAt ?: Long.MAX_VALUE },
    { atom -> progress[atom.atom_id]?.mastery ?: 0f },
    CatalogAtom::order_index,
    CatalogAtom::atom_id,
)

/**
 * An in-place APK update can replace the bundled pack while Room/DataStore
 * correctly retain IDs from the old pack. Never let those stale IDs turn the
 * first wake on the new pack into a quiz for a lesson the user has not seen.
 */
internal fun reconcilePendingContentState(
    old: LockCycleEntity,
    pendingContentValid: Boolean,
    now: Long,
): LockCycleEntity {
    if (pendingContentValid) return old
    val pauseStillActive = old.mode == StudyCoordinator.PAUSED &&
        (old.pausedUntil ?: Long.MIN_VALUE) > now
    return old.copy(
        mode = if (pauseStillActive) StudyCoordinator.PAUSED else StudyCoordinator.READY_LESSON,
        pendingPairId = null,
        currentAtomId = null,
        currentLessonId = null,
        currentQuestionId = null,
        quizRoundId = null,
        quizPurpose = StudyCoordinator.LEARNING_BUNDLE,
        activeUnlockSessionId = null,
        deviceInactive = false,
        lastScreenOffAt = null,
    )
}

/** A mistake may only interrupt at the clean boundary between two complete knowledge units. */
internal fun spacedReviewBoundaryEligible(state: LockCycleEntity): Boolean =
    state.mode == StudyCoordinator.READY_LESSON &&
        state.pendingPairId == null &&
        state.currentAtomId == null &&
        state.currentLessonId == null &&
        state.currentQuestionId == null &&
        state.quizRoundId == null &&
        state.activeUnlockSessionId == null &&
        !state.normalPairRequiredAfterReview

/** Disabling delivery drops the visible item, but never forgives an outstanding normal-pair debt. */
internal fun nextLockCycleAfterDisable(
    old: LockCycleEntity,
    now: Long,
): LockCycleEntity = old.copy(
    mode = StudyCoordinator.DISABLED,
    pendingPairId = null,
    currentAtomId = null,
    currentLessonId = null,
    currentQuestionId = null,
    quizRoundId = null,
    quizPurpose = StudyCoordinator.LEARNING_BUNDLE,
    activeUnlockSessionId = null,
    deviceInactive = false,
    pausedUntil = null,
    lastEventType = "DISABLE",
    lastEventAt = now,
    serviceHeartbeatAt = now,
    revision = old.revision + 1,
)

/** A failed reminder stays pinned; a correct reminder yields to one complete normal pair. */
internal fun nextLockCycleAfterSpacedReview(
    old: LockCycleEntity,
    correct: Boolean,
): LockCycleEntity {
    require(old.quizPurpose == StudyCoordinator.SPACED_MISTAKE) {
        "Only a spaced-mistake quiz can use the spaced-review transition"
    }
    require(!old.currentQuestionId.isNullOrBlank() && !old.quizRoundId.isNullOrBlank()) {
        "A spaced review needs a persisted question and round"
    }
    return if (correct) {
        old.copy(
            mode = StudyCoordinator.READY_LESSON,
            pendingPairId = null,
            currentAtomId = null,
            currentLessonId = null,
            currentQuestionId = null,
            quizRoundId = null,
            quizPurpose = StudyCoordinator.LEARNING_BUNDLE,
            normalPairRequiredAfterReview = true,
            activeUnlockSessionId = null,
            revision = old.revision + 1,
        )
    } else {
        old.copy(
            mode = StudyCoordinator.READY_QUIZ,
            activeUnlockSessionId = null,
            revision = old.revision + 1,
        )
    }
}

/** Returns the first declared quiz not yet answered correctly in this round. */
internal fun nextRequiredQuestionId(
    orderedQuestionIds: List<String>,
    correctlyAnsweredIds: Set<String>,
): String? {
    require(orderedQuestionIds.isNotEmpty()) { "A quiz round needs at least one question" }
    require(orderedQuestionIds.distinct().size == orderedQuestionIds.size) {
        "Quiz question IDs must be unique"
    }
    return orderedQuestionIds.firstOrNull { it !in correctlyAnsweredIds }
}

/** Keep the exact atom pinned until no required question remains. */
internal fun nextLockCycleAfterQuizAnswer(
    old: LockCycleEntity,
    nextQuestionId: String?,
): LockCycleEntity {
    if (nextQuestionId == null && (old.pendingPairId == null || old.quizRoundId == null)) return old
    return if (nextQuestionId == null) {
        old.copy(
            mode = StudyCoordinator.READY_LESSON,
            pendingPairId = null,
            currentAtomId = null,
            currentLessonId = null,
            currentQuestionId = null,
            quizRoundId = null,
            quizPurpose = StudyCoordinator.LEARNING_BUNDLE,
            normalPairRequiredAfterReview = false,
            activeUnlockSessionId = null,
            successfulCycles = old.successfulCycles + 1,
            revision = old.revision + 1,
        )
    } else {
        require(!old.quizRoundId.isNullOrBlank()) { "An unfinished bundle requires a quiz round" }
        old.copy(
            mode = StudyCoordinator.READY_QUIZ,
            currentQuestionId = nextQuestionId,
            activeUnlockSessionId = null,
            revision = old.revision + 1,
        )
    }
}
