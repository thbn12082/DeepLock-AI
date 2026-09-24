package com.example.deeplock.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.semantics.progressBarRangeInfo
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

/** A simple game-style bar without Material's end marker at zero progress. */
@Composable
fun GameProgressIndicator(
    progress: Float,
    modifier: Modifier = Modifier,
) {
    val normalized = progress.coerceIn(0f, 1f)
    val shape = RoundedCornerShape(999.dp)
    Box(
        modifier
            .semantics { progressBarRangeInfo = ProgressBarRangeInfo(normalized, 0f..1f) }
            .height(8.dp)
            .clip(shape)
            .background(MaterialTheme.colorScheme.surfaceContainerHighest.copy(alpha = 0.58f)),
    ) {
        if (normalized > 0f) {
            Box(
                Modifier
                    .fillMaxHeight()
                    .fillMaxWidth(normalized)
                    .clip(shape)
                    .background(MaterialTheme.colorScheme.primary),
            )
        }
    }
}
