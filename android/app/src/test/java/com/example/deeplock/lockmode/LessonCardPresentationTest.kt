package com.example.deeplock.lockmode

import com.example.deeplock.data.content.FormulaBlock
import com.example.deeplock.data.content.FormulaSymbol
import com.example.deeplock.data.content.Illustration
import com.example.deeplock.data.content.Lesson
import com.example.deeplock.data.content.TinyExample
import com.google.common.truth.Truth.assertThat
import org.junit.Test

class LessonCardPresentationTest {
    @Test fun pagesKeepEveryLessonFieldWithoutTruncation() {
        val lesson = lesson()
        val visuals = listOf(illustration("visual-a"), illustration("visual-b"))

        val pages = buildLessonCardPages(lesson, visuals)

        assertThat(pages.map { it.id }).containsExactly(
            "overview",
            "intuition-0",
            "visual-0-visual-a",
            "visual-1-visual-b",
            "example",
            "formula-0",
            "formula-1",
            "process-0",
            "mistake",
            "takeaways-0",
            "recall",
        ).inOrder()
        assertThat(pages.map { it.id }.toSet()).hasSize(pages.size)
        val overview = pages.filterIsInstance<LessonCardPage.Overview>().single()
        assertThat(overview.objective).isEqualTo(lesson.learning_objective)
        assertThat(overview.hook).isEqualTo(lesson.hook)
        assertThat(pages.filterIsInstance<LessonCardPage.Intuition>().joinToString("") { it.text })
            .isEqualTo(lesson.intuition)
        assertThat(pages.filterIsInstance<LessonCardPage.Visual>().map { it.illustration })
            .containsExactlyElementsIn(visuals).inOrder()
        assertThat(pages.filterIsInstance<LessonCardPage.Example>().single().example)
            .isEqualTo(lesson.tiny_example)
        assertThat(pages.filterIsInstance<LessonCardPage.Formula>().map { it.formula })
            .containsExactlyElementsIn(lesson.formula_blocks).inOrder()
        assertThat(pages.filterIsInstance<LessonCardPage.Process>().flatMap { it.steps })
            .containsExactlyElementsIn(lesson.process_steps).inOrder()
        assertThat(pages.filterIsInstance<LessonCardPage.Mistake>().single().text)
            .isEqualTo(lesson.common_mistake)
        assertThat(pages.filterIsInstance<LessonCardPage.Takeaways>().flatMap { it.items })
            .containsExactlyElementsIn(lesson.takeaways).inOrder()
        assertThat(pages.filterIsInstance<LessonCardPage.Recall>().single().prompt)
            .isEqualTo(lesson.recall_prompt)
    }

    @Test fun absentVisualFormulaAndProcessSectionsDoNotCreatePlaceholderPages() {
        val lesson = lesson().copy(formula_blocks = emptyList(), process_steps = emptyList())

        val pages = buildLessonCardPages(lesson, emptyList())

        assertThat(pages.map { it::class }).containsExactly(
            LessonCardPage.Overview::class,
            LessonCardPage.Intuition::class,
            LessonCardPage.Example::class,
            LessonCardPage.Mistake::class,
            LessonCardPage.Takeaways::class,
            LessonCardPage.Recall::class,
        ).inOrder()
    }

    @Test fun processAndTakeawayChunksPreserveOrderAndGlobalNumbering() {
        val processSteps = (1..8).map { "Process $it" }
        val takeaways = (1..7).map { "Takeaway $it" }

        val pages = buildLessonCardPages(
            lesson().copy(process_steps = processSteps, takeaways = takeaways),
            emptyList(),
        )

        val processPages = pages.filterIsInstance<LessonCardPage.Process>()
        assertThat(processPages.map { it.id })
            .containsExactly("process-0", "process-1", "process-2").inOrder()
        assertThat(processPages.map { it.startNumber }).containsExactly(1, 4, 7).inOrder()
        assertThat(processPages.map { it.steps.size }).containsExactly(3, 3, 2).inOrder()
        assertThat(processPages.flatMap { it.steps }).containsExactlyElementsIn(processSteps).inOrder()

        val takeawayPages = pages.filterIsInstance<LessonCardPage.Takeaways>()
        assertThat(takeawayPages.map { it.id })
            .containsExactly("takeaways-0", "takeaways-1", "takeaways-2").inOrder()
        assertThat(takeawayPages.map { it.startNumber }).containsExactly(1, 4, 7).inOrder()
        assertThat(takeawayPages.map { it.items.size }).containsExactly(3, 3, 1).inOrder()
        assertThat(takeawayPages.flatMap { it.items }).containsExactlyElementsIn(takeaways).inOrder()
    }

    @Test fun longTextIsSplitIntoShortPagesWithoutChangingOneCharacter() {
        val original = (1..40).joinToString(" ") { "đoạn-$it" }

        val pages = splitLessonText(original, targetChars = 80)

        assertThat(pages.size).isGreaterThan(1)
        assertThat(pages).doesNotContain("")
        assertThat(pages.all { it.length <= 80 }).isTrue()
        assertThat(pages.joinToString("")).isEqualTo(original)
    }

    @Test fun textWithoutWhitespaceStillSplitsLosslesslyAtTheRequestedLimit() {
        val original = "x".repeat(201)

        val pages = splitLessonText(original, targetChars = 80)

        assertThat(pages.map { it.length }).containsExactly(80, 80, 41).inOrder()
        assertThat(pages.joinToString("")).isEqualTo(original)
    }

    @Test fun emptyTextCreatesNoContentPageAndExactLimitStaysOnOnePage() {
        assertThat(splitLessonText("", targetChars = 80)).isEmpty()
        assertThat(splitLessonText("x".repeat(80), targetChars = 80))
            .containsExactly("x".repeat(80))
    }

    @Test fun onlyTheFinalPageCanCompleteTheLesson() {
        val count = 7

        repeat(count - 1) { index ->
            assertThat(lessonPrimaryAction(index, count)).isEqualTo(LessonPrimaryAction.NEXT)
        }
        assertThat(lessonPrimaryAction(count - 1, count)).isEqualTo(LessonPrimaryAction.COMPLETE)
    }

    @Test fun backMovesWithinLessonAndDefersOnlyFromTheFirstPage() {
        assertThat(lessonBackAction(3)).isEqualTo(LessonBackAction.PREVIOUS)
        assertThat(lessonBackAction(0)).isEqualTo(LessonBackAction.DEFER)
        assertThat(clampLessonPageIndex(99, 6)).isEqualTo(5)
        assertThat(clampLessonPageIndex(-4, 6)).isEqualTo(0)
    }

    @Test fun stablePageIdRestoresTheExactPageAndUnknownIdsStartAtTheBeginning() {
        val pages = buildLessonCardPages(lesson(), listOf(illustration("visual-a")))
        val formulaPage = pages.single { it.id == "formula-1" }

        assertThat(resolveLessonPageIndex(formulaPage.id, pages))
            .isEqualTo(pages.indexOf(formulaPage))
        assertThat(resolveLessonPageIndex("stale-page-id", pages)).isEqualTo(0)
        assertThat(resolveLessonPageIndex(null, pages)).isEqualTo(0)
    }

    private fun lesson() = Lesson(
        lesson_id = "lesson-a",
        pair_id = "pair-a",
        atom_id = "atom-a",
        title = "A complete lesson",
        learning_objective = "Objective that must stay complete",
        grounding_type = "SOURCE_GROUNDED",
        prerequisite_ids = emptyList(),
        hook = "Hook that must stay complete",
        intuition = "Intuition that must stay complete",
        tiny_example = TinyExample(
            input = "Full input",
            steps = listOf("Full step one", "Full step two"),
            output = "Full output",
        ),
        process_steps = listOf("Process one", "Process two"),
        formula_blocks = listOf(
            FormulaBlock("x", "x = 1", listOf(FormulaSymbol("x", "first symbol"))),
            FormulaBlock("y", "y = 2", listOf(FormulaSymbol("y", "second symbol"))),
        ),
        common_mistake = "Complete common mistake",
        takeaways = listOf("Takeaway one", "Takeaway two"),
        recall_prompt = "Complete recall prompt",
        estimated_seconds = 60,
        source_ref_ids = listOf("source-a"),
    )

    private fun illustration(id: String) = Illustration(
        illustration_id = id,
        source_ref_id = "source-$id",
        asset_member = "assets/$id.png",
        mime_type = "image/png",
        sha256 = id.padEnd(64, '0'),
        byte_size = 100,
        width = 640,
        height = 480,
        page_number = 1,
        caption = "Caption $id",
        alt_text = "Alt $id",
    )
}
