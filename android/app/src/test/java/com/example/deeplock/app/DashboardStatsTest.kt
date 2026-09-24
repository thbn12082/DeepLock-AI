package com.example.deeplock.app

import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.AttemptActivityRow
import com.example.deeplock.data.local.LessonActivityRow
import com.example.deeplock.data.local.MistakeEntity
import com.google.common.truth.Truth.assertThat
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import org.junit.Test

class DashboardStatsTest {
    @Test
    fun buildsSevenLocalCalendarDaysAndCountsDistinctLessons() {
        val stats = buildDashboardStats(
            nowMillis = at(2026, 8, 25, 12, 0),
            zoneId = ZONE,
            knownAtomIds = setOf("atom-a", "atom-b"),
            knownQuestionIds = setOf("q-a", "q-b"),
            progress = listOf(
                AtomProgressEntity("atom-a", status = "MASTERED"),
                AtomProgressEntity("stale-atom", status = "MASTERED"),
            ),
            mistakes = listOf(
                mistake("q-a", dueAt = at(2026, 8, 25, 10, 0)),
                mistake("q-b", dueAt = at(2026, 8, 26, 10, 0)),
                mistake("stale-question", dueAt = 1L),
            ),
            attemptActivity = listOf(
                AttemptActivityRow(at(2026, 8, 25, 8, 0), correct = true),
                AttemptActivityRow(at(2026, 8, 25, 9, 0), correct = false),
                AttemptActivityRow(at(2026, 8, 24, 23, 0), correct = false),
            ),
            lessonActivity = listOf(
                LessonActivityRow(at(2026, 8, 25, 7, 0), "lesson-a"),
                LessonActivityRow(at(2026, 8, 25, 7, 5), "lesson-a"),
                LessonActivityRow(at(2026, 8, 25, 10, 0), "lesson-b"),
                LessonActivityRow(at(2026, 8, 18, 10, 0), "outside-chart"),
            ),
        )

        assertThat(stats.days).hasSize(7)
        assertThat(stats.days.first().date).isEqualTo(LocalDate.of(2026, 8, 19))
        assertThat(stats.days.last().date).isEqualTo(LocalDate.of(2026, 8, 25))
        assertThat(stats.lessonsCompletedToday).isEqualTo(2)
        assertThat(stats.questionsAnsweredToday).isEqualTo(2)
        assertThat(stats.days.last().wrongAttempts).isEqualTo(1)
        assertThat(stats.knownMistakeQuestions).isEqualTo(2)
        assertThat(stats.dueMistakeQuestions).isEqualTo(1)
        assertThat(stats.masteredKnownAtoms).isEqualTo(1)
        assertThat(stats.totalKnownAtoms).isEqualTo(2)
        assertThat(stats.totalStudyActions).isEqualTo(3)
        assertThat(stats.averageStudyActions).isWithin(0.0001f).of(3f / 7f)
    }

    @Test
    fun midnightBoundaryUsesDeviceCalendarDayAndZeroFillsMissingDays() {
        val stats = buildDashboardStats(
            nowMillis = at(2026, 8, 25, 0, 30),
            zoneId = ZONE,
            knownAtomIds = emptySet(),
            knownQuestionIds = emptySet(),
            progress = emptyList(),
            mistakes = emptyList(),
            attemptActivity = listOf(
                AttemptActivityRow(at(2026, 8, 24, 23, 59), correct = true),
                AttemptActivityRow(at(2026, 8, 25, 0, 0), correct = false),
            ),
            lessonActivity = emptyList(),
        )

        assertThat(stats.days.dropLast(2).all { it.studyActions == 0 }).isTrue()
        assertThat(stats.days[5].questionsAnswered).isEqualTo(1)
        assertThat(stats.days[5].wrongAttempts).isEqualTo(0)
        assertThat(stats.days[6].questionsAnswered).isEqualTo(1)
        assertThat(stats.days[6].wrongAttempts).isEqualTo(1)
    }

    @Test
    fun malformedAndOutOfRangeRowsCannotPolluteVisibleStats() {
        val stats = buildDashboardStats(
            nowMillis = at(2026, 8, 25, 12, 0),
            zoneId = ZONE,
            knownAtomIds = setOf("atom-a"),
            knownQuestionIds = setOf("q-a"),
            progress = listOf(AtomProgressEntity("atom-a", status = "IN_PROGRESS")),
            mistakes = listOf(mistake("q-a", dueAt = -5L)),
            attemptActivity = listOf(AttemptActivityRow(-1L, correct = false)),
            lessonActivity = listOf(
                LessonActivityRow(at(2026, 8, 25, 8, 0), ""),
                LessonActivityRow(-1L, "lesson-a"),
            ),
        )

        assertThat(stats.totalStudyActions).isEqualTo(0)
        assertThat(stats.dueMistakeQuestions).isEqualTo(0)
        assertThat(stats.masteredKnownAtoms).isEqualTo(0)
        assertThat(stats.averageStudyActions).isEqualTo(0f)
    }

    private fun mistake(questionId: String, dueAt: Long) = MistakeEntity(
        questionId = questionId,
        atomId = "atom-a",
        errorCount = 1,
        lastSelectedOptionId = "B",
        lastSeenAt = dueAt.coerceAtLeast(0L),
        dueAt = dueAt,
    )

    private fun at(
        year: Int,
        month: Int,
        day: Int,
        hour: Int,
        minute: Int,
    ): Long = LocalDateTime.of(year, month, day, hour, minute)
        .atZone(ZONE)
        .toInstant()
        .toEpochMilli()

    private companion object {
        val ZONE: ZoneId = ZoneId.of("Asia/Bangkok")
    }
}
