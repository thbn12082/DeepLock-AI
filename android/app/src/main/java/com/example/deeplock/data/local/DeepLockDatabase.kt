package com.example.deeplock.data.local

import androidx.room.Dao
import androidx.room.Database
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import kotlinx.coroutines.flow.Flow

@Dao
interface StudyDao {
    @Query("SELECT * FROM atom_progress ORDER BY atomId") fun observeProgress(): Flow<List<AtomProgressEntity>>
    @Query("SELECT * FROM mistakes ORDER BY dueAt, lastSeenAt DESC") fun observeMistakes(): Flow<List<MistakeEntity>>
    @Query("SELECT * FROM attempts ORDER BY createdAt DESC LIMIT :limit") fun observeAttempts(limit: Int = 100): Flow<List<AttemptEntity>>
    @Query(
        """
        SELECT createdAt, correct
        FROM attempts
        WHERE createdAt >= :fromInclusive
        ORDER BY createdAt
        """,
    )
    fun observeAttemptActivitySince(fromInclusive: Long): Flow<List<AttemptActivityRow>>
    @Query(
        """
        SELECT createdAt,
               COALESCE(NULLIF(pairId, ''), NULLIF(itemId, ''), CAST(id AS TEXT)) AS lessonKey
        FROM study_events
        WHERE type = 'lesson_completed' AND createdAt >= :fromInclusive
        ORDER BY createdAt
        """,
    )
    fun observeLessonActivitySince(fromInclusive: Long): Flow<List<LessonActivityRow>>
    @Query("SELECT * FROM atom_progress ORDER BY mastery, dueAt, atomId") suspend fun progressNow(): List<AtomProgressEntity>
    @Query("SELECT * FROM atom_progress WHERE atomId = :atomId") suspend fun progress(atomId: String): AtomProgressEntity?
    @Query("SELECT * FROM attempts WHERE pairId = :pairId ORDER BY createdAt DESC") suspend fun attemptsForPair(pairId: String): List<AttemptEntity>
    @Query("SELECT COUNT(*) FROM attempts WHERE attemptUid = :attemptUid") suspend fun attemptUidCount(attemptUid: String): Int
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun putProgress(value: AtomProgressEntity)
    @Insert suspend fun addAttempt(value: AttemptEntity): Long
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun putMistake(value: MistakeEntity)
    @Query("SELECT * FROM mistakes WHERE questionId = :questionId") suspend fun mistake(questionId: String): MistakeEntity?
    @Query("SELECT * FROM mistakes WHERE pairId = :pairId ORDER BY lastSeenAt DESC") suspend fun mistakesForPair(pairId: String): List<MistakeEntity>
    @Query(
        """
        SELECT m.*
        FROM mistakes AS m
        INNER JOIN atom_progress AS p ON p.atomId = m.atomId
        WHERE p.status = 'MASTERED' AND m.dueAt > 0 AND m.dueAt <= :now
        ORDER BY m.dueAt ASC, m.errorCount DESC, m.firstSeenAt ASC, m.questionId ASC
        """,
    )
    suspend fun dueMasteredMistakes(now: Long): List<MistakeEntity>
    @Query("UPDATE mistakes SET userNote = :note WHERE questionId = :questionId") suspend fun updateMistakeNote(questionId: String, note: String)
    @Query("DELETE FROM attempts") suspend fun clearAttempts()
    @Query("DELETE FROM mistakes") suspend fun clearMistakes()
    @Query("DELETE FROM atom_progress") suspend fun clearProgress()
}

@Dao
interface LockCycleDao {
    @Query("SELECT * FROM lock_cycle WHERE singletonId = 1") suspend fun get(): LockCycleEntity?
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun put(value: LockCycleEntity)
    @Insert suspend fun insertSession(value: UnlockSessionEntity)
    @Query("SELECT * FROM unlock_sessions WHERE unlockSessionId = :id") suspend fun session(id: String): UnlockSessionEntity?
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun putSession(value: UnlockSessionEntity)
    @Query("SELECT * FROM unlock_sessions ORDER BY screenOnAt DESC LIMIT :limit") suspend fun recentSessions(limit: Int = 100): List<UnlockSessionEntity>
    @Query("SELECT COUNT(*) FROM unlock_sessions WHERE screenOnAt >= :fromInclusive") suspend fun sessionCountSince(fromInclusive: Long): Int
    @Query("SELECT COALESCE(SUM(notifyCount), 0) FROM unlock_sessions WHERE screenOnAt >= :fromInclusive") suspend fun notifyCountSince(fromInclusive: Long): Int
    @Insert suspend fun addEvent(value: StudyEventEntity)
    @Query("SELECT * FROM study_events ORDER BY createdAt DESC LIMIT :limit") suspend fun recentEvents(limit: Int = 100): List<StudyEventEntity>
    @Query("DELETE FROM study_events") suspend fun clearEvents()
    @Query("SELECT * FROM lock_question_progress WHERE quizRoundId = :quizRoundId AND questionId = :questionId")
    suspend fun questionProgress(quizRoundId: String, questionId: String): LockQuestionProgressEntity?
    @Query("SELECT questionId FROM lock_question_progress WHERE quizRoundId = :quizRoundId AND correct = 1")
    suspend fun correctQuestionIds(quizRoundId: String): List<String>
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putQuestionProgress(value: LockQuestionProgressEntity)
    @Query("DELETE FROM lock_question_progress WHERE quizRoundId = :quizRoundId")
    suspend fun deleteQuestionProgress(quizRoundId: String)
    @Query("DELETE FROM lock_question_progress") suspend fun clearQuestionProgress()
}

@Dao
interface QuizSessionDao {
    @Query("SELECT * FROM quiz_sessions WHERE sessionId = :id") suspend fun get(id: String): QuizSessionEntity?
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun put(value: QuizSessionEntity)
    @Query("DELETE FROM quiz_sessions WHERE sessionId = :id") suspend fun delete(id: String)
}

@Dao
interface WidgetSnapshotDao {
    @Query("SELECT * FROM widget_snapshot WHERE singletonId = 1") suspend fun get(): WidgetSnapshotEntity?
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun put(value: WidgetSnapshotEntity)
}

@Database(
    entities = [
        AtomProgressEntity::class,
        AttemptEntity::class,
        MistakeEntity::class,
        LockCycleEntity::class,
        LockQuestionProgressEntity::class,
        UnlockSessionEntity::class,
        QuizSessionEntity::class,
        StudyEventEntity::class,
        WidgetSnapshotEntity::class,
    ],
    version = 4,
    exportSchema = true,
)
abstract class DeepLockDatabase : RoomDatabase() {
    abstract fun studyDao(): StudyDao
    abstract fun lockCycleDao(): LockCycleDao
    abstract fun quizSessionDao(): QuizSessionDao
    abstract fun widgetSnapshotDao(): WidgetSnapshotDao

    companion object {
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE atom_progress ADD COLUMN dueAt INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE atom_progress ADD COLUMN leitnerBox INTEGER NOT NULL DEFAULT 1")
                db.execSQL("ALTER TABLE atom_progress ADD COLUMN lastStudiedAt INTEGER")
                db.execSQL("ALTER TABLE atom_progress ADD COLUMN bookmarked INTEGER NOT NULL DEFAULT 0")

                db.execSQL("ALTER TABLE attempts ADD COLUMN attemptUid TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE attempts ADD COLUMN pairId TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE attempts ADD COLUMN preRecallMs INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE attempts ADD COLUMN preRecallSkipped INTEGER NOT NULL DEFAULT 0")

                db.execSQL("ALTER TABLE mistakes ADD COLUMN pairId TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN correctOptionId TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN selectedRationale TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN status TEXT NOT NULL DEFAULT 'UNREVIEWED'")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN userNote TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN firstSeenAt INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN improvedAt INTEGER")

                db.execSQL("UPDATE lock_cycle SET state='DISABLED', activePairId=NULL")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN currentAtomId TEXT")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN currentLessonId TEXT")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN currentQuestionId TEXT")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN lastScreenOffAt INTEGER")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN lastScreenOnAt INTEGER")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN activeUnlockSessionId TEXT")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN pausedUntil INTEGER")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN deviceInactive INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN lastEventType TEXT")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN serviceHeartbeatAt INTEGER")

                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS unlock_sessions (
                        unlockSessionId TEXT NOT NULL PRIMARY KEY,
                        screenOffAt INTEGER NOT NULL,
                        screenOnAt INTEGER NOT NULL,
                        keyguardLocked INTEGER NOT NULL,
                        pairId TEXT NOT NULL,
                        atomId TEXT NOT NULL,
                        itemType TEXT NOT NULL,
                        itemId TEXT NOT NULL,
                        questionId TEXT,
                        surface TEXT NOT NULL,
                        notifyCount INTEGER NOT NULL DEFAULT 0,
                        notificationPostedAt INTEGER,
                        userPresentAt INTEGER,
                        outcome TEXT
                    )
                """.trimIndent())
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS quiz_sessions (
                        sessionId TEXT NOT NULL PRIMARY KEY,
                        questionId TEXT NOT NULL,
                        pairId TEXT NOT NULL,
                        atomId TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        preRecallStartedAt INTEGER NOT NULL,
                        optionsVisibleAt INTEGER,
                        preRecallSkipped INTEGER NOT NULL DEFAULT 0,
                        selectedOptionId TEXT,
                        submittedAttemptUid TEXT,
                        updatedAt INTEGER NOT NULL
                    )
                """.trimIndent())
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS study_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                        type TEXT NOT NULL,
                        createdAt INTEGER NOT NULL,
                        unlockSessionId TEXT,
                        pairId TEXT,
                        itemId TEXT,
                        surface TEXT,
                        detail TEXT
                    )
                """.trimIndent())
                db.execSQL("""
                    CREATE TABLE IF NOT EXISTS widget_snapshot (
                        singletonId INTEGER NOT NULL PRIMARY KEY,
                        dueCount INTEGER NOT NULL,
                        weakAtomId TEXT,
                        weakAtomTitle TEXT,
                        continueAtomId TEXT,
                        continueAtomTitle TEXT,
                        updatedAt INTEGER NOT NULL
                    )
                """.trimIndent())
            }
        }

        val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN quizRoundId TEXT")
                db.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS lock_question_progress (
                        quizRoundId TEXT NOT NULL,
                        questionId TEXT NOT NULL,
                        pairId TEXT NOT NULL,
                        atomId TEXT NOT NULL,
                        correct INTEGER NOT NULL,
                        wrongCount INTEGER NOT NULL,
                        lastAttemptAt INTEGER NOT NULL,
                        correctAt INTEGER,
                        PRIMARY KEY(quizRoundId, questionId)
                    )
                    """.trimIndent(),
                )
                // Older builds advanced to a quiz merely because the lesson became visible.
                // Re-show that same pending lesson once; no learning history is deleted.
                db.execSQL(
                    """
                    UPDATE lock_cycle
                    SET state = CASE
                            WHEN state IN ('READY_QUIZ', 'QUIZ_RETRY') THEN 'READY_LESSON'
                            ELSE state
                        END,
                        currentQuestionId = NULL,
                        quizRoundId = NULL,
                        activeUnlockSessionId = NULL,
                        deviceInactive = 0,
                        lastEventType = 'MIGRATE_V3_RESHOW_LESSON'
                    """.trimIndent(),
                )
            }
        }

        val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE mistakes ADD COLUMN reviewBox INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE mistakes ADD COLUMN lastReviewedAt INTEGER")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN quizPurpose TEXT NOT NULL DEFAULT 'LEARNING_BUNDLE'")
                db.execSQL("ALTER TABLE lock_cycle ADD COLUMN normalPairRequiredAfterReview INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE unlock_sessions ADD COLUMN quizPurpose TEXT")
                db.execSQL(
                    """
                    UPDATE unlock_sessions
                    SET quizPurpose = CASE
                        WHEN itemType = 'QUIZ' THEN 'LEARNING_BUNDLE'
                        ELSE NULL
                    END
                    """.trimIndent(),
                )
                db.execSQL("CREATE INDEX IF NOT EXISTS index_attempts_createdAt ON attempts(createdAt)")
                db.execSQL("CREATE INDEX IF NOT EXISTS index_mistakes_dueAt_questionId ON mistakes(dueAt, questionId)")
                db.execSQL("CREATE INDEX IF NOT EXISTS index_study_events_type_createdAt ON study_events(type, createdAt)")
            }
        }
    }
}
