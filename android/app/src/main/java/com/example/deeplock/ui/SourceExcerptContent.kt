package com.example.deeplock.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import com.example.deeplock.data.content.SourceExcerpt

/**
 * Presentation-only model for an excerpt embedded in a lesson. [contextText] is
 * always the complete pack payload; the UI must never shorten it or set maxLines.
 */
data class SourceExcerptUiModel(
    val sourceExcerptId: String,
    val documentLabel: String,
    val pageLabel: String,
    val headingLabel: String?,
    val segmentIndex: Int,
    val segmentCount: Int,
    val contextText: String,
    val highlightStart: Int,
    val highlightEnd: Int,
) {
    val hasValidHighlight: Boolean
        get() = highlightStart >= 0 &&
            highlightStart < contextText.length &&
            highlightEnd > highlightStart &&
            highlightEnd <= contextText.length
}

/**
 * Keeps the lesson's declared source order, then page order within each source.
 * Every matching source excerpt is returned exactly once and with unchanged text.
 */
fun buildSourceExcerptUiModels(
    sourceRefIds: List<String>,
    excerpts: Collection<SourceExcerpt>,
): List<SourceExcerptUiModel> {
    val excerptsBySource = excerpts.groupBy(SourceExcerpt::source_ref_id)
    return sourceRefIds.distinct().flatMap { sourceRefId ->
        val sourceExcerpts = excerptsBySource[sourceRefId]
            .orEmpty()
            .sortedWith(
                compareBy<SourceExcerpt>({ it.page_start }, { it.page_end }, { it.source_excerpt_id }),
            )
        sourceExcerpts.mapIndexed { index, excerpt ->
            SourceExcerptUiModel(
                sourceExcerptId = excerpt.source_excerpt_id,
                documentLabel = excerpt.document_label.ifBlank { excerpt.document_id },
                pageLabel = if (excerpt.page_start == excerpt.page_end) {
                    "Trang ${excerpt.page_start}"
                } else {
                    "Trang ${excerpt.page_start}–${excerpt.page_end}"
                },
                headingLabel = excerpt.heading_path
                    .filter(String::isNotBlank)
                    .joinToString(" › ")
                    .ifBlank { null },
                segmentIndex = index + 1,
                segmentCount = sourceExcerpts.size,
                contextText = excerpt.context_text,
                highlightStart = excerpt.highlight_start,
                highlightEnd = excerpt.highlight_end,
            )
        }
    }
}

@Composable
fun FullSourceExcerptHeader(excerptCount: Int, modifier: Modifier = Modifier) {
    Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("Nội dung nguồn đầy đủ", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(
            if (excerptCount == 0) {
                "Bài học này chưa có đoạn trích nguồn trong gói nội dung."
            } else {
                "$excerptCount đoạn trích · giữ nguyên toàn bộ nội dung, không rút gọn."
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
fun FullSourceExcerptCard(item: SourceExcerptUiModel, modifier: Modifier = Modifier) {
    val highlightColor = MaterialTheme.colorScheme.primaryContainer
    Card(modifier.fillMaxWidth()) {
        Column(
            Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(item.documentLabel, fontWeight = FontWeight.Bold)
            Text(
                "${item.pageLabel} · Đoạn ${item.segmentIndex}/${item.segmentCount}",
                color = MaterialTheme.colorScheme.primary,
                style = MaterialTheme.typography.labelLarge,
            )
            item.headingLabel?.let { heading ->
                Text(heading, style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold)
            }
            HorizontalDivider()
            if (item.hasValidHighlight) {
                Text(buildAnnotatedString {
                    append(item.contextText.substring(0, item.highlightStart))
                    withStyle(
                        SpanStyle(
                            background = highlightColor,
                            fontWeight = FontWeight.Medium,
                        ),
                    ) {
                        append(item.contextText.substring(item.highlightStart, item.highlightEnd))
                    }
                    append(item.contextText.substring(item.highlightEnd))
                })
            } else {
                // Invalid highlight metadata must never hide or shorten source text.
                Text(item.contextText)
            }
        }
    }
}
