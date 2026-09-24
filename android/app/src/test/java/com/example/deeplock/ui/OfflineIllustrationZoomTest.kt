package com.example.deeplock.ui

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class OfflineIllustrationZoomTest {
    @Test
    fun clampsScaleToSupportedRangeAndRejectsNonFiniteInput() {
        assertThat(transform(scale = 0.2f).scale).isEqualTo(1f)
        assertThat(transform(scale = 8f).scale).isEqualTo(5f)
        assertThat(transform(scale = Float.NaN).scale).isEqualTo(1f)
    }

    @Test
    fun resetsOffsetsWhenFittedImageDoesNotOverflowViewport() {
        val result = transform(
            scale = 1f,
            offsetX = 900f,
            offsetY = -900f,
        )

        assertThat(result.offsetX).isEqualTo(0f)
        assertThat(result.offsetY).isEqualTo(0f)
    }

    @Test
    fun clampsPanToScaledImageEdges() {
        val result = transform(
            scale = 3f,
            offsetX = 9_000f,
            offsetY = -9_000f,
        )

        // A 400x200 image fits a 1000x1000 viewport as 1000x500.
        assertThat(result.offsetX).isEqualTo(1_000f)
        assertThat(result.offsetY).isEqualTo(-250f)
    }

    @Test
    fun invalidViewportCannotLeaveImagePannedOffScreen() {
        val result = clampIllustrationZoomTransform(
            requestedScale = 2f,
            requestedOffsetX = 200f,
            requestedOffsetY = 300f,
            viewportWidth = 0f,
            viewportHeight = 1_000f,
            imageWidth = 400f,
            imageHeight = 200f,
        )

        assertThat(result).isEqualTo(IllustrationZoomTransform(scale = 2f))
    }

    private fun transform(
        scale: Float,
        offsetX: Float = 0f,
        offsetY: Float = 0f,
    ): IllustrationZoomTransform = clampIllustrationZoomTransform(
        requestedScale = scale,
        requestedOffsetX = offsetX,
        requestedOffsetY = offsetY,
        viewportWidth = 1_000f,
        viewportHeight = 1_000f,
        imageWidth = 400f,
        imageHeight = 200f,
    )
}
