package com.example.deeplock.ui

import com.example.deeplock.data.content.SourceExcerpt
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class SourceExcerptContentTest {
    @Test
    fun keepsEveryMatchingExcerptInDeclaredSourceAndPageOrderWithoutTruncatingText() {
        val fullContext = (1..500).joinToString("\n\n") { paragraph ->
            "Đoạn $paragraph giữ nguyên nội dung bài giảng và không bị rút gọn."
        }
        val excerpts = listOf(
            excerpt(id = "a-late", source = "source-a", pageStart = 12, pageEnd = 13, text = "A sau"),
            excerpt(id = "unowned", source = "source-c", pageStart = 1, text = "Không thuộc bài học"),
            excerpt(id = "b", source = "source-b", pageStart = 7, text = fullContext),
            excerpt(id = "a-early", source = "source-a", pageStart = 3, text = "A trước"),
        )

        val result = buildSourceExcerptUiModels(
            sourceRefIds = listOf("source-b", "source-a", "source-b"),
            excerpts = excerpts,
        )

        assertThat(result.map { it.sourceExcerptId })
            .containsExactly("b", "a-early", "a-late")
            .inOrder()
        assertThat(result.first().contextText).isEqualTo(fullContext)
        assertThat(result.first().contextText.length).isEqualTo(fullContext.length)
        assertThat(result[1].segmentIndex).isEqualTo(1)
        assertThat(result[1].segmentCount).isEqualTo(2)
        assertThat(result[2].segmentIndex).isEqualTo(2)
        assertThat(result[2].segmentCount).isEqualTo(2)
    }

    @Test
    fun exposesClearPageHeadingAndDocumentLabels() {
        val result = buildSourceExcerptUiModels(
            sourceRefIds = listOf("source-a", "source-b"),
            excerpts = listOf(
                excerpt(
                    id = "range",
                    source = "source-a",
                    pageStart = 4,
                    pageEnd = 6,
                    documentLabel = "Lecture MLOps.pdf",
                    headingPath = listOf("MLOps", "", "Model serving"),
                ),
                excerpt(
                    id = "single",
                    source = "source-b",
                    pageStart = 9,
                    documentLabel = "",
                    documentId = "document-fallback",
                ),
            ),
        )

        assertThat(result[0].documentLabel).isEqualTo("Lecture MLOps.pdf")
        assertThat(result[0].pageLabel).isEqualTo("Trang 4–6")
        assertThat(result[0].headingLabel).isEqualTo("MLOps › Model serving")
        assertThat(result[1].documentLabel).isEqualTo("document-fallback")
        assertThat(result[1].pageLabel).isEqualTo("Trang 9")
        assertThat(result[1].headingLabel).isNull()
    }

    @Test
    fun invalidHighlightMetadataStillKeepsTheCompleteContextVisible() {
        val text = "Toàn bộ ngữ cảnh nguồn phải luôn được hiển thị."
        val valid = buildSourceExcerptUiModels(
            listOf("valid"),
            listOf(excerpt("valid-id", "valid", 1, text = text, highlightStart = 9, highlightEnd = 17)),
        ).single()
        val invalid = buildSourceExcerptUiModels(
            listOf("invalid"),
            listOf(excerpt("invalid-id", "invalid", 1, text = text, highlightStart = 9, highlightEnd = text.length + 1)),
        ).single()

        assertThat(valid.hasValidHighlight).isTrue()
        assertThat(invalid.hasValidHighlight).isFalse()
        assertThat(invalid.contextText).isEqualTo(text)
    }

    private fun excerpt(
        id: String,
        source: String,
        pageStart: Int,
        pageEnd: Int = pageStart,
        text: String = "Nội dung nguồn",
        documentLabel: String = "Lecture.pdf",
        documentId: String = "document-id",
        headingPath: List<String> = emptyList(),
        highlightStart: Int = 0,
        highlightEnd: Int = text.length,
    ) = SourceExcerpt(
        source_excerpt_id = id,
        source_ref_id = source,
        document_id = documentId,
        chunk_id = "chunk-$id",
        document_label = documentLabel,
        heading_path = headingPath,
        page_start = pageStart,
        page_end = pageEnd,
        context_text = text,
        highlight_start = highlightStart,
        highlight_end = highlightEnd,
        excerpt_hash = "sha256:test-$id",
    )
}
