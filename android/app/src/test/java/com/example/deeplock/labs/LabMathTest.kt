package com.example.deeplock.labs

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class LabMathTest {
    @Test fun gradientStepUsesDescentDirection() {
        assertThat(LabMath.gradientStep(2.0, .5, .1)).isWithin(1e-9).of(1.95)
    }

    @Test fun activationsAreDeterministic() {
        assertThat(LabMath.activation("RELU", -2.0)).isEqualTo(0.0)
        assertThat(LabMath.activation("SIGMOID", 0.0)).isWithin(1e-9).of(.5)
        assertThat(LabMath.activation("TANH", 0.0)).isEqualTo(0.0)
    }

    @Test fun convolutionComputesValidWindows() {
        val result = LabMath.convolution(
            listOf(listOf(1.0, 2.0), listOf(3.0, 4.0)),
            listOf(listOf(1.0, 0.0), listOf(0.0, -1.0)),
            1,
        )
        assertThat(result).containsExactly(listOf(-3.0))
    }

    @Test fun detectsFirstDivergingEpoch() {
        assertThat(LabMath.firstOverfitEpoch(listOf(.9, .7, .5), listOf(.8, .6, .7))).isEqualTo(3)
    }

    @Test fun confusionMetricsAreCorrect() {
        val value = LabMath.confusionMetrics(listOf(listOf(8, 2), listOf(1, 9)))
        assertThat(value.accuracy).isWithin(1e-9).of(.85)
        assertThat(value.macroRecall).isWithin(1e-9).of(.85)
    }

    @Test fun nonFiniteAndOutOfBoundsMathIsRejected() {
        assertThat(runCatching { LabMath.gradientStep(Double.NaN, 1.0, .1) }.isFailure).isTrue()
        assertThat(runCatching { LabMath.activation("RELU", 21.0) }.isFailure).isTrue()
        assertThat(runCatching {
            LabMath.convolution(List(10) { listOf(1.0) }, listOf(listOf(1.0)), 1)
        }.isFailure).isTrue()
        assertThat(runCatching { LabMath.confusionMetrics(listOf(listOf(1, -1), listOf(0, 1))) }.isFailure).isTrue()
    }
}
