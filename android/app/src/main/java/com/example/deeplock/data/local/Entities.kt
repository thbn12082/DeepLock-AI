package com.example.deeplock.data.local

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey
import java.util.UUID

@Entity(tableName = "atom_progress")
data class AtomProgressEntity(
    @PrimaryKey val atomId: String,
    val status: String = "NEW",
    val mastery: Float = 0f,
    val completedAt: Long? = null,
    @ColumnInfo(defaultValue = "0") val dueAt: Long = 0L,
    @ColumnInfo(defaultValue = "1") val leitnerBox: Int = 1,
    val lastStudiedAt: Long? = null,
    @ColumnInfo(defaultValue = "0") val bookmarked: Boolean = false,
)

@Entity(
    tableName = "attempts",
    indices = [Index(value = ["createdAt"])],
)
data class AttemptEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val questionId: String,
    val atomId: String,
    val selectedOptionId: String,
    val correct: Boolean,
    val origin: String,
    val createdAt: Long,
    @ColumnInfo(defaultValue = "''") val attemptUid: String = UUID.randomUUID().toString(),
    @ColumnInfo(defaultValue = "''") val pairId: String = "",
    @ColumnInfo(defaultValue = "0") val preRecallMs: Long = 0L,
    @ColumnInfo(defaultValue = "0") val preRecallSkipped: Boolean = false,
)

@Entity(
    tableName = "mistakes",
    indices = [Index(value = ["dueAt", "questionId"])],
)
data class MistakeEntity(
    @PrimaryKey val questionId: String,
    val atomId: String,
    val errorCount: Int,
    val lastSelectedOptionId: String,
    val lastSeenAt: Long,
    val dueAt: Long,
    @ColumnInfo(defaultValue = "''") val pairId: String = "",
    @ColumnInfo(defaultValue = "''") val correctOptionId: String = "",
    @ColumnInfo(defaultValue = "''") val selectedRationale: String = "",
    @ColumnInfo(defaultValue = "'UNREVIEWED'") val status: String = "UNREVIEWED",
    @ColumnInfo(defaultValue = "''") val userNote: String = "",
    @ColumnInfo(defaultValue = "0") val firstSeenAt: Long = 0L,
    val improvedAt: Long? = null,
    @ColumnInfo(defaultValue = "0") val reviewBox: Int = 0,
    val lastReviewedAt: Long? = null,
)

/**
 * The v1 column names `state` and `activePairId` are retained so an installed
 * debug APK can migrate in place. Kotlin exposes the contract names instead.
 */
@Entity(tableName = "lock_cycle")
data class LockCycleEntity(
    @PrimaryKey val singletonId: Int = 1,
    @ColumnInfo(name = "state") val mode: String = "DISABLED",
    @ColumnInfo(name = "activePairId") val pendingPairId: String? = null,
    val lastEventAt: Long = 0L,
    val successfulCycles: Int = 0,
    val currentAtomId: String? = null,
    val currentLessonId: String? = null,
    val currentQuestionId: String? = null,
    val quizRoundId: String? = null,
    val lastScreenOffAt: Long? = null,
    val lastScreenOnAt: Long? = null,
    val activeUnlockSessionId: String? = null,
    val pausedUntil: Long? = null,
    @ColumnInfo(defaultValue = "0") val revision: Long = 0L,
    @ColumnInfo(defaultValue = "0") val deviceInactive: Boolean = false,
    val lastEventType: String? = null,
    val serviceHeartbeatAt: Long? = null,
    @ColumnInfo(defaultValue = "'LEARNING_BUNDLE'") val quizPurpose: String = "LEARNING_BUNDLE",
    @ColumnInfo(defaultValue = "0") val normalPairRequiredAfterReview: Boolean = false,
)

@Entity(
    tableName = "lock_question_progress",
    primaryKeys = ["quizRoundId", "questionId"],
)
data class LockQuestionProgressEntity(
    val quizRoundId: String,
    val questionId: String,
    val pairId: String,
    val atomId: String,
    val correct: Boolean,
    val wrongCount: Int,
    val lastAttemptAt: Long,
    val correctAt: Long? = null,
)

@Entity(tableName = "unlock_sessions")
data class UnlockSessionEntity(
    @PrimaryKey val unlockSessionId: String,
    val screenOffAt: Long,
    val screenOnAt: Long,
    val keyguardLocked: Boolean,
    val pairId: String,
    val atomId: String,
    val itemType: String,
    val itemId: String,
    val questionId: String? = null,
    val surface: String,
    @ColumnInfo(defaultValue = "0") val notifyCount: Int = 0,
    val notificationPostedAt: Long? = null,
    val userPresentAt: Long? = null,
    val outcome: String? = null,
    val quizPurpose: String? = null,
)

@Entity(tableName = "quiz_sessions")
data class QuizSessionEntity(
    @PrimaryKey val sessionId: String,
    val questionId: String,
    val pairId: String,
    val atomId: String,
    val origin: String,
    val phase: String = "PRE_RECALL",
    val preRecallStartedAt: Long,
    val optionsVisibleAt: Long? = null,
    @ColumnInfo(defaultValue = "0") val preRecallSkipped: Boolean = false,
    val selectedOptionId: String? = null,
    val submittedAttemptUid: String? = null,
    val updatedAt: Long,
)

@Entity(
    tableName = "study_events",
    indices = [Index(value = ["type", "createdAt"])],
)
data class StudyEventEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val type: String,
    val createdAt: Long,
    val unlockSessionId: String? = null,
    val pairId: String? = null,
    val itemId: String? = null,
    val surface: String? = null,
    val detail: String? = null,
)

data class AttemptActivityRow(
    val createdAt: Long,
    val correct: Boolean,
)

data class LessonActivityRow(
    val createdAt: Long,
    val lessonKey: String,
)

@Entity(tableName = "widget_snapshot")
data class WidgetSnapshotEntity(
    @PrimaryKey val singletonId: Int = 1,
    val dueCount: Int = 0,
    val weakAtomId: String? = null,
    val weakAtomTitle: String? = null,
    val continueAtomId: String? = null,
    val continueAtomTitle: String? = null,
    val updatedAt: Long = 0L,
)
