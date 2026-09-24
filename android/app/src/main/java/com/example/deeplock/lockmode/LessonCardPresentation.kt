package com.example.deeplock.lockmode

import com.example.deeplock.data.content.FormulaBlock
import com.example.deeplock.data.content.Illustration
import com.example.deeplock.data.content.Lesson
import com.example.deeplock.data.content.TinyExample

/** One focused screen at a time; no lesson field is shortened or discarded. */
internal sealed interface LessonCardPage {
    val id: String
    val title: String

    data class Overview(
        val objective: String,
        val hook: String,
    ) : LessonCardPage {
        override val id = "overview"
        override val title = "Ý chính"
    }

    data class Intuition(val index: Int, val text: String) : LessonCardPage {
        override val id = "intuition-$index"
        override val title = "Hiểu nhanh"
    }

    data class Visual(
        val index: Int,
        val illustration: Illustration,
    ) : LessonCardPage {
        override val id = "visual-$index-${illustration.illustration_id}"
        override val title = "Minh họa"
    }

    data class Example(val example: TinyExample) : LessonCardPage {
        override val id = "example"
        override val title = "Ví dụ"
    }

    data class Formula(
        val index: Int,
        val formula: FormulaBlock,
    ) : LessonCardPage {
        override val id = "formula-$index"
        override val title = "Công thức"
    }

    data class Process(
        val groupIndex: Int,
        val startNumber: Int,
        val steps: List<String>,
    ) : LessonCardPage {
        override val id = "process-$groupIndex"
        override val title = "Cách làm"
    }

    data class Mistake(val text: String) : LessonCardPage {
        override val id = "mistake"
        override val title = "Tránh bẫy"
    }

    data class Takeaways(
        val groupIndex: Int,
        val startNumber: Int,
        val items: List<String>,
    ) : LessonCardPage {
        override val id = "takeaways-$groupIndex"
        override val title = "Ghi nhớ"
    }

    data class Recall(val prompt: String) : LessonCardPage {
        override val id = "recall"
        override val title = "Chốt màn"
    }
}

internal fun buildLessonCardPages(
    lesson: Lesson,
    illustrations: List<Illustration>,
): List<LessonCardPage> = buildList {
    add(LessonCardPage.Overview(lesson.learning_objective, lesson.hook))
    splitLessonText(lesson.intuition).forEachIndexed { index, text ->
        add(LessonCardPage.Intuition(index, text))
    }
    illustrations.forEachIndexed { index, illustration ->
        add(LessonCardPage.Visual(index, illustration))
    }
    add(LessonCardPage.Example(lesson.tiny_example))
    lesson.formula_blocks.forEachIndexed { index, formula ->
        add(LessonCardPage.Formula(index, formula))
    }
    lesson.process_steps.chunked(STEPS_PER_PAGE).forEachIndexed { index, steps ->
        add(LessonCardPage.Process(index, index * STEPS_PER_PAGE + 1, steps))
    }
    if (lesson.common_mistake.isNotBlank()) add(LessonCardPage.Mistake(lesson.common_mistake))
    lesson.takeaways.chunked(STEPS_PER_PAGE).forEachIndexed { index, items ->
        add(LessonCardPage.Takeaways(index, index * STEPS_PER_PAGE + 1, items))
    }
    if (lesson.recall_prompt.isNotBlank()) add(LessonCardPage.Recall(lesson.recall_prompt))
}

/** Splits long prose into game-sized screens while concatenating back losslessly. */
internal fun splitLessonText(text: String, targetChars: Int = TARGET_TEXT_CHARS): List<String> {
    require(targetChars >= MIN_TEXT_CHARS) { "Text pages cannot be smaller than $MIN_TEXT_CHARS characters" }
    if (text.isEmpty()) return emptyList()
    val pages = mutableListOf<String>()
    var start = 0
    while (start < text.length) {
        if (text.length - start <= targetChars) {
            pages += text.substring(start)
            break
        }
        val proposedEnd = (start + targetChars).coerceAtMost(text.length)
        val lowerBound = start + targetChars / 2
        var end = proposedEnd
        for (candidate in proposedEnd downTo lowerBound) {
            if (text[candidate - 1].isWhitespace()) {
                end = candidate
                break
            }
        }
        pages += text.substring(start, end)
        start = end
    }
    return pages
}

internal enum class LessonPrimaryAction { NEXT, COMPLETE }
internal enum class LessonBackAction { PREVIOUS, DEFER }

internal fun lessonPrimaryAction(pageIndex: Int, pageCount: Int): LessonPrimaryAction {
    require(pageCount > 0) { "A lesson needs at least one page" }
    require(pageIndex in 0 until pageCount) { "Page index is outside the lesson" }
    return if (pageIndex == pageCount - 1) LessonPrimaryAction.COMPLETE else LessonPrimaryAction.NEXT
}

internal fun lessonBackAction(pageIndex: Int): LessonBackAction {
    require(pageIndex >= 0) { "Page index cannot be negative" }
    return if (pageIndex == 0) LessonBackAction.DEFER else LessonBackAction.PREVIOUS
}

internal fun clampLessonPageIndex(pageIndex: Int, pageCount: Int): Int {
    require(pageCount > 0) { "A lesson needs at least one page" }
    return pageIndex.coerceIn(0, pageCount - 1)
}

internal fun resolveLessonPageIndex(pageId: String?, pages: List<LessonCardPage>): Int {
    require(pages.isNotEmpty()) { "A lesson needs at least one page" }
    return pages.indexOfFirst { it.id == pageId }.takeIf { it >= 0 } ?: 0
}

private const val STEPS_PER_PAGE = 3
private const val TARGET_TEXT_CHARS = 360
private const val MIN_TEXT_CHARS = 80
