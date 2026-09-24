package com.example.deeplock.ui

import androidx.compose.ui.graphics.Color
import com.google.common.truth.Truth.assertThat
import kotlin.math.pow
import org.junit.Test

class DeepLockThemeTest {
    @Test
    fun notebookPaletteKeepsCoreTextAndControlsReadable() {
        assertThat(contrast(NotebookColorScheme.onBackground, NotebookColorScheme.background))
            .isAtLeast(4.5)
        assertThat(contrast(NotebookColorScheme.onSurface, NotebookColorScheme.surface))
            .isAtLeast(4.5)
        assertThat(contrast(NotebookColorScheme.onPrimary, NotebookColorScheme.primary))
            .isAtLeast(4.5)
        assertThat(contrast(NotebookColorScheme.primary, NotebookColorScheme.background))
            .isAtLeast(4.5)
        assertThat(contrast(NotebookColorScheme.outline, NotebookColorScheme.background))
            .isAtLeast(3.0)
    }

    private fun contrast(first: Color, second: Color): Double {
        val lighter = maxOf(luminance(first), luminance(second))
        val darker = minOf(luminance(first), luminance(second))
        return (lighter + 0.05) / (darker + 0.05)
    }

    private fun luminance(color: Color): Double =
        0.2126 * linear(color.red) +
            0.7152 * linear(color.green) +
            0.0722 * linear(color.blue)

    private fun linear(component: Float): Double {
        val value = component.toDouble()
        return if (value <= 0.04045) value / 12.92 else ((value + 0.055) / 1.055).pow(2.4)
    }
}
