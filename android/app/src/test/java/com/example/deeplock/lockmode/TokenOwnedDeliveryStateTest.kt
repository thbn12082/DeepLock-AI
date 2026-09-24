package com.example.deeplock.lockmode

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class TokenOwnedDeliveryStateTest {
    @Test fun freshRequestStaysHiddenUntilItsExactTokenIsAccepted() {
        val pending = TokenOwnedDeliveryState<String>().receive(
            token = "token-a",
            value = "lesson-a",
            alreadyAccepted = false,
        )!!

        assertThat(pending.pending?.value).isEqualTo("lesson-a")
        assertThat(pending.visible).isNull()
        assertThat(pending.accept("token-old", "lesson-a")).isEqualTo(pending)

        val accepted = pending.accept("token-a", "lesson-a")
        assertThat(accepted.visible?.value).isEqualTo("lesson-a")
        assertThat(accepted.ownsVisible("token-a", "lesson-a")).isTrue()
    }

    @Test fun freshTokenHidesOldCardAndLateOldAcceptanceCannotRevealIt() {
        val acceptedA = TokenOwnedDeliveryState<String>()
            .receive("token-a", "lesson-a", alreadyAccepted = false)!!
            .accept("token-a", "lesson-a")

        val pendingB = acceptedA.receive(
            token = "token-b",
            value = "quiz-b",
            alreadyAccepted = false,
        )!!

        assertThat(pendingB.visible).isNull()
        assertThat(pendingB.ownsVisible("token-a", "lesson-a")).isFalse()
        assertThat(pendingB.accept("token-a", "lesson-a")).isEqualTo(pendingB)
        assertThat(pendingB.accept("token-b", "quiz-b").visible?.value).isEqualTo("quiz-b")
    }

    @Test fun staleCallbacksCannotOwnNewVisibleRequest() {
        val acceptedA = TokenOwnedDeliveryState<String>()
            .receive("token-a", "lesson-a", alreadyAccepted = true)!!
        val acceptedB = acceptedA.receive(
            token = "token-b",
            value = "quiz-b",
            alreadyAccepted = true,
        )!!

        assertThat(acceptedB.ownsVisible("token-a", "lesson-a")).isFalse()
        assertThat(acceptedB.ownsVisible("token-b", "lesson-a")).isFalse()
        assertThat(acceptedB.ownsVisible("token-b", "quiz-b")).isTrue()
    }

    @Test fun recreatedAlreadyAcceptedTokenCanRenderWithoutAnotherHandshake() {
        val restored = TokenOwnedDeliveryState<String>().receive(
            token = "token-a",
            value = "quiz-a",
            alreadyAccepted = true,
        )!!

        assertThat(restored.ownsPending("token-a", "quiz-a")).isTrue()
        assertThat(restored.ownsVisible("token-a", "quiz-a")).isTrue()
    }

    @Test fun reusedTokenWithDifferentPayloadFailsClosed() {
        val pending = TokenOwnedDeliveryState<String>().receive(
            token = "token-a",
            value = "lesson-a",
            alreadyAccepted = false,
        )!!

        assertThat(
            pending.receive(
                token = "token-a",
                value = "different-payload",
                alreadyAccepted = true,
            ),
        ).isNull()
        assertThat(pending.accept("token-a", "different-payload")).isEqualTo(pending)
    }

    @Test fun retainedAcceptanceRequiresTheFullRequestNotOnlyItsToken() {
        val lesson = request(token = "token-a", lessonId = "lesson-a")
        val changedPayload = request(token = "token-a", lessonId = "lesson-b")
        val retained = DirectDeliveryViewModel().apply {
            markIncoming(lesson)
            markForegroundReported(lesson)
            markAccepted(lesson)
        }

        assertThat(retained.isAccepted(lesson)).isTrue()
        assertThat(retained.isAccepted(changedPayload)).isFalse()
        assertThat(retained.canReceive(changedPayload)).isFalse()
    }

    @Test fun newerTokenRevokesAcceptanceOfAnExactOldRequestReplay() {
        val oldRequest = request(token = "token-a", lessonId = "lesson-a")
        val newRequest = request(token = "token-b", lessonId = "lesson-b")
        val retained = DirectDeliveryViewModel().apply {
            markIncoming(oldRequest)
            markForegroundReported(oldRequest)
            markAccepted(oldRequest)
        }

        retained.markIncoming(newRequest)

        assertThat(retained.isAccepted(oldRequest)).isFalse()
        assertThat(retained.wasForegroundReported(oldRequest)).isFalse()
        assertThat(retained.isAccepted(newRequest)).isFalse()
    }

    private fun request(token: String, lessonId: String) = DirectCardRequest(
        attemptToken = token,
        sessionId = "session-a",
        pairId = "pair-a",
        atomId = "atom-a",
        lessonId = lessonId,
        questionId = null,
        itemType = com.example.deeplock.data.LockItemType.LESSON,
    )
}
