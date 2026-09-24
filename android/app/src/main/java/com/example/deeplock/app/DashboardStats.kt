package com.example.deeplock.app

import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.AttemptActivityRow
import com.example.deeplock.data.local.LessonActivityRow
import com.example.deeplock.data.local.MistakeEntity
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

internal data class DashboardDayStats(
    val date: LocalDate,
    val lessonsCompleted: Int,
    val questionsAnswered: Int,
    val wrongAttempts: Int,
) {
    /** One comparable unit for every day; lessons remain a separate dashboard metric. */
    val studyActions: Int get() = questionsAnswered
}

internal data class DashboardStats(
    val knownMistakeQuestions: Int,
    val dueMistakeQuestions: Int,
    val lessonsCompletedToday: Int,
    val questionsAnsweredToday: Int,
    val correctAnswersToday: Int,
    val wrongAnswersToday: Int,
    val masteredKnownAtoms: Int,
    val totalKnownAtoms: Int,
    val days: List<DashboardDayStats>,
) {
    val totalStudyActions: Int get() = days.sumOf(DashboardDayStats::studyActions)
    val averageStudyActions: Float get() = totalStudyActions / DAYS_IN_CHART.toFloat()
    val todayAccuracyPercent: Int get() =
        if (questionsAnsweredToday == 0) 0 else (correctAnswersToday * 100f / questionsAnsweredToday).toInt()
    val sevenDayStreak: Int get() =
        days.asReversed().takeWhile { it.studyActions > 0 || it.lessonsCompleted > 0 }.count()
    val masteredPercent: Float get() =
        if (totalKnownAtoms == 0) 0f else masteredKnownAtoms.toFloat() / totalKnownAtoms
}

internal fun buildDashboardStats(
    nowMillis: Long,
    zoneId: ZoneId,
    knownAtomIds: Set<String>,
    knownQuestionIds: Set<String>,
    progress: Collection<AtomProgressEntity>,
    mistakes: Collection<MistakeEntity>,
    attemptActivity: Collection<AttemptActivityRow>,
    lessonActivity: Collection<LessonActivityRow>,
): DashboardStats {
    val today = localDate(nowMillis, zoneId)
    val chartDates = (DAYS_IN_CHART - 1 downTo 0).map { offset ->
        today.minusDays(offset.toLong())
    }
    val chartDateSet = chartDates.toSet()

    val attemptsByDate = attemptActivity
        .asSequence()
        .filter { it.createdAt >= 0L }
        .groupBy { localDate(it.createdAt, zoneId) }
    val lessonsByDate = lessonActivity
        .asSequence()
        .filter { it.createdAt >= 0L && it.lessonKey.isNotBlank() }
        .groupBy { localDate(it.createdAt, zoneId) }

    val days = chartDates.map { date ->
        val attempts = attemptsByDate[date].orEmpty()
        DashboardDayStats(
            date = date,
            lessonsCompleted = lessonsByDate[date]
                .orEmpty()
                .asSequence()
                .map(LessonActivityRow::lessonKey)
                .distinct()
                .count(),
            questionsAnswered = attempts.size,
            wrongAttempts = attempts.count { !it.correct },
        )
    }
    check(days.map(DashboardDayStats::date).toSet() == chartDateSet)

    val knownMistakes = mistakes.filter { it.questionId in knownQuestionIds }
    val todayStats = days.last()
    return DashboardStats(
        knownMistakeQuestions = knownMistakes.asSequence().map(MistakeEntity::questionId).distinct().count(),
        dueMistakeQuestions = knownMistakes.count { it.dueAt in 1..nowMillis },
        lessonsCompletedToday = todayStats.lessonsCompleted,
        questionsAnsweredToday = todayStats.questionsAnswered,
        correctAnswersToday = todayStats.questionsAnswered - todayStats.wrongAttempts,
        wrongAnswersToday = todayStats.wrongAttempts,
        masteredKnownAtoms = progress.asSequence()
            .filter { it.atomId in knownAtomIds && it.status == MASTERED_STATUS }
            .map(AtomProgressEntity::atomId)
            .distinct()
            .count(),
        totalKnownAtoms = knownAtomIds.size,
        days = days,
    )
}

private fun localDate(epochMillis: Long, zoneId: ZoneId): LocalDate =
    Instant.ofEpochMilli(epochMillis).atZone(zoneId).toLocalDate()

private const val DAYS_IN_CHART = 7
private const val MASTERED_STATUS = "MASTERED"
