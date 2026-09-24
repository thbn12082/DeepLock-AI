package com.example.deeplock.widget

import com.example.deeplock.data.local.WidgetSnapshotEntity
import com.example.deeplock.lockmode.LockNotificationController
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class WidgetSnapshotCalculatorTest {
    @Test fun snapshotHasCorrectDueWeakAndContinueValues() {
        val snapshot = WidgetSnapshotCalculator.calculate(
            atoms = listOf(
                atom("new", "Chưa học", order = 3, status = "NEW", mastery = 0f),
                atom("a", "Atom A", order = 1, mastery = .7f, dueAt = NOW - 1L, lastStudiedAt = 100L),
                atom("d", "Atom D", order = 4, status = "MASTERED", mastery = .9f, dueAt = NOW - 2L, lastStudiedAt = 300L),
                atom("b", "Atom B", order = 2, mastery = .2f, dueAt = NOW + 1L, lastStudiedAt = 200L),
            ),
            now = NOW,
        )

        assertThat(snapshot.dueCount).isEqualTo(2)
        assertThat(snapshot.weakAtomId).isEqualTo("b")
        assertThat(snapshot.weakAtomTitle).isEqualTo("Atom B")
        assertThat(snapshot.continueAtomId).isEqualTo("b")
        assertThat(snapshot.continueAtomTitle).isEqualTo("Atom B")
        assertThat(snapshot.updatedAt).isEqualTo(NOW)
    }

    @Test fun freshCourseHasContinueTargetButNoInventedWeakAtom() {
        val snapshot = WidgetSnapshotCalculator.calculate(
            atoms = listOf(
                atom("b", "B", order = 2, status = "NEW", mastery = 0f),
                atom("a", "A", order = 1, status = "NEW", mastery = 0f),
            ),
            now = NOW,
        )

        assertThat(snapshot.dueCount).isEqualTo(0)
        assertThat(snapshot.weakAtomId).isNull()
        assertThat(snapshot.continueAtomId).isEqualTo("a")
    }

    @Test fun calculationNormalizesMalformedNumbersAndBreaksTiesDeterministically() {
        val snapshot = WidgetSnapshotCalculator.calculate(
            atoms = listOf(
                atom("z", "Z", order = Int.MIN_VALUE, mastery = Float.NaN, dueAt = -9L, lastStudiedAt = -1L),
                atom("a", "A", order = Int.MIN_VALUE, mastery = Float.NaN, dueAt = -1L, lastStudiedAt = -2L),
            ).reversed(),
            now = NOW,
        )

        assertThat(snapshot.dueCount).isEqualTo(0)
        assertThat(snapshot.weakAtomId).isEqualTo("a")
        assertThat(snapshot.continueAtomId).isEqualTo("a")
    }

    @Test fun countsOnlyHidesAllTitlesButKeepsSafeActions() {
        val presentation = WidgetSnapshotCalculator.present(
            snapshot = snapshot(),
            showTitles = false,
            validAtomIds = setOf("weak", "continue"),
        )

        assertThat(presentation.dueCount).isEqualTo(3)
        assertThat(presentation.weakLine).doesNotContain("Secret")
        assertThat(presentation.continueLine).isNull()
        assertThat(presentation.learnTarget).isEqualTo(
            WidgetTarget(LockNotificationController.DEST_HOME, null),
        )
        assertThat(presentation.reviewTarget).isEqualTo(
            WidgetTarget(LockNotificationController.DEST_HOME, null),
        )
    }

    @Test fun titlesModeShowsTitlesButStillRoutesOnlyToStatistics() {
        val presentation = WidgetSnapshotCalculator.present(
            snapshot = snapshot(),
            showTitles = true,
            validAtomIds = setOf("weak", "continue"),
        )

        assertThat(presentation.weakLine).contains("Secret weak")
        assertThat(presentation.continueLine).isEqualTo("Secret continue")
        assertThat(presentation.learnTarget.destination).isEqualTo(LockNotificationController.DEST_HOME)
        assertThat(presentation.reviewTarget.destination).isEqualTo(LockNotificationController.DEST_HOME)
        assertThat(presentation.learnTarget.atomId).isNull()
        assertThat(presentation.reviewTarget.atomId).isNull()
    }

    @Test fun staleIdsFallBackHomeAndNeverExposeStaleTitles() {
        val presentation = WidgetSnapshotCalculator.present(
            snapshot = snapshot(),
            showTitles = true,
            validAtomIds = emptySet(),
        )

        assertThat(presentation.weakLine).doesNotContain("Secret")
        assertThat(presentation.continueLine).isNull()
        assertThat(presentation.learnTarget).isEqualTo(
            WidgetTarget(LockNotificationController.DEST_HOME, null),
        )
        assertThat(presentation.reviewTarget).isEqualTo(
            WidgetTarget(LockNotificationController.DEST_HOME, null),
        )
    }

    @Test fun corruptNegativeCountIsClamped() {
        val presentation = WidgetSnapshotCalculator.present(
            snapshot = WidgetSnapshotEntity(dueCount = -4),
            showTitles = true,
            validAtomIds = emptySet(),
        )

        assertThat(presentation.dueCount).isEqualTo(0)
    }

    private fun snapshot() = WidgetSnapshotEntity(
        dueCount = 3,
        weakAtomId = "weak",
        weakAtomTitle = "Secret weak",
        continueAtomId = "continue",
        continueAtomTitle = "Secret continue",
        updatedAt = NOW,
    )

    private fun atom(
        id: String,
        title: String,
        order: Int,
        status: String = "IN_PROGRESS",
        mastery: Float,
        dueAt: Long = 0L,
        lastStudiedAt: Long? = null,
    ) = WidgetAtomState(
        atomId = id,
        title = title,
        order = order,
        status = status,
        mastery = mastery,
        dueAt = dueAt,
        lastStudiedAt = lastStudiedAt,
    )

    private companion object {
        const val NOW = 10_000L
    }
}
