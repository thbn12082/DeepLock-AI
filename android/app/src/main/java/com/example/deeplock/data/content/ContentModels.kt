package com.example.deeplock.data.content

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable data class Course(val course_id: String, val title: String, val description: String, val language: String)

/**
 * Small, eagerly loaded index for the offline library. Heavy lesson/question/image payloads stay
 * in independently verified dlpack assets and are opened only when their lecture is selected.
 */
@Serializable
data class ContentCatalog(
    val schema_version: String,
    val catalog_id: String,
    val title: String,
    val language: String,
    val default_pack_id: String,
    val categories: List<CatalogCategory>,
    val lectures: List<CatalogLecture>,
    val packs: List<CatalogPack>,
    val atoms: List<CatalogAtom> = emptyList(),
) {
    private val categoryByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { categories.associateBy { it.category_id } }
    private val lectureByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { lectures.associateBy { it.lecture_id } }
    private val packByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { packs.associateBy { it.pack_id } }
    private val lecturesByCategoryIndex by lazy(LazyThreadSafetyMode.PUBLICATION) {
        lectures.groupBy { it.category_id }.mapValues { (_, values) ->
            values.sortedWith(compareBy<CatalogLecture>({ it.order_index }, { it.lecture_id }))
        }
    }
    private val packsByLectureIndex by lazy(LazyThreadSafetyMode.PUBLICATION) {
        packs.groupBy { it.lecture_id }.mapValues { (_, values) ->
            values.sortedWith(compareBy<CatalogPack>({ it.order_index }, { it.pack_id }))
        }
    }
    private val atomsByPackIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { atoms.groupBy { it.pack_id } }
    private val atomByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { atoms.associateBy { it.atom_id } }
    private val atomByPairIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { atoms.associateBy { it.pair_id } }
    private val atomByLessonIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { atoms.associateBy { it.lesson_id } }
    private val atomByQuestionIndex by lazy(LazyThreadSafetyMode.PUBLICATION) {
        buildMap { atoms.forEach { atom -> atom.question_ids.forEach { put(it, atom) } } }
    }
    private val orderedAtomsIndex by lazy(LazyThreadSafetyMode.PUBLICATION) {
        atoms.sortedWith(compareBy<CatalogAtom>({ it.order_index }, { it.atom_id }))
    }

    fun categoryById(id: String) = categoryByIdIndex[id]
    fun lectureById(id: String) = lectureByIdIndex[id]
    fun packById(id: String) = packByIdIndex[id]
    fun atomById(id: String) = atomByIdIndex[id]
    fun atomByPair(id: String) = atomByPairIndex[id]
    fun atomByLesson(id: String) = atomByLessonIndex[id]
    fun atomByQuestion(id: String) = atomByQuestionIndex[id]
    fun packIdForAtom(id: String) = atomById(id)?.pack_id
    fun packIdForPair(id: String) = atomByPair(id)?.pack_id
    fun packIdForQuestion(id: String) = atomByQuestion(id)?.pack_id
    fun packsForLecture(id: String): List<CatalogPack> = packsByLectureIndex[id].orEmpty()
    fun lecturesForCategory(id: String): List<CatalogLecture> = lecturesByCategoryIndex[id].orEmpty()
    fun atomsForPack(id: String): List<CatalogAtom> = atomsByPackIndex[id].orEmpty()
    fun orderedAtoms(): List<CatalogAtom> = orderedAtomsIndex
}

@Serializable
data class CatalogCategory(
    val category_id: String,
    val title: String,
    val description: String = "",
    val order_index: Int,
    val search_terms: List<String> = emptyList(),
)

@Serializable
data class CatalogLecture(
    val lecture_id: String,
    val category_id: String,
    val title: String,
    val description: String = "",
    val order_index: Int,
    val search_terms: List<String> = emptyList(),
)

@Serializable
data class CatalogPack(
    val pack_id: String,
    val lecture_id: String,
    val asset_path: String,
    val title: String,
    val order_index: Int,
    val course_id: String,
    val asset_sha256: String? = null,
)

@Serializable
data class CatalogAtom(
    val pack_id: String,
    val atom_id: String,
    val pair_id: String,
    val lesson_id: String,
    val module_id: String,
    val title: String,
    /** Global catalog order, not the atom's module-local order_index. */
    val order_index: Int,
    val prerequisite_ids: List<String>,
    val question_ids: List<String>,
)

@Serializable
data class AtomPlan(
    val atom_id: String,
    val order_index: Int,
    val title: String,
    val objective: String,
    val prerequisite_ids: List<String>,
    val source_ref_ids: List<String>,
)

@Serializable
data class Module(
    val module_id: String,
    val order_index: Int,
    val title: String,
    val objective: String,
    val atom_plans: List<AtomPlan>,
)

@Serializable
data class KnowledgeAtom(
    val atom_id: String,
    val module_id: String,
    val order_index: Int,
    val title: String,
    val grounding_type: String,
    val prerequisite_ids: List<String>,
    val estimated_seconds: Int,
)

@Serializable data class TinyExample(val input: String, val steps: List<String>, val output: String)
@Serializable data class FormulaSymbol(val symbol: String, val meaning: String)
@Serializable data class FormulaBlock(val latex: String, val plain_text: String, val symbols: List<FormulaSymbol>)

@Serializable
data class Lesson(
    val lesson_id: String,
    val pair_id: String,
    val atom_id: String,
    val title: String,
    val learning_objective: String,
    val grounding_type: String,
    val prerequisite_ids: List<String>,
    val hook: String,
    val intuition: String,
    val tiny_example: TinyExample,
    val process_steps: List<String>,
    val formula_blocks: List<FormulaBlock>,
    val common_mistake: String,
    val takeaways: List<String>,
    val recall_prompt: String,
    val estimated_seconds: Int,
    val source_ref_ids: List<String>,
    val illustration_source_ref_ids: List<String> = emptyList(),
)

@Serializable
data class LearningPair(
    val pair_id: String,
    val atom_id: String,
    val micro_lesson_id: String,
    val question_ids: List<String>,
    val revision: Int,
    val content_hash: String,
    val ai_review_status: String,
)

@Serializable data class QuizOption(val id: String, val text: String, val rationale: String, val misconception_tag: String?)

@Serializable
data class Question(
    val question_id: String,
    val pair_id: String,
    val atom_id: String,
    val stem: String,
    val options: List<QuizOption>,
    val correct_option_id: String,
    val explanation: String,
    val difficulty: Int,
    val bloom_level: String,
    val grounding_type: String,
    val source_ref_ids: List<String>,
)

@Serializable data class MiniLabFallback(val columns: List<String>, val rows: List<List<String>>, val takeaways: List<String>)

@Serializable
data class MiniLab(
    val mini_lab_id: String,
    val atom_id: String,
    val lab_type: String,
    val spec: JsonObject,
    val fallback: MiniLabFallback,
    val source_ref_ids: List<String>,
    val ai_review_status: String,
)

@Serializable
data class SourceRef(
    val source_ref_id: String,
    val document_id: String,
    val chunk_id: String,
    val page_start: Int,
    val page_end: Int,
    val anchor_start: Int,
    val anchor_end: Int,
    val claim_type: String,
)

@Serializable
data class SourceExcerpt(
    val source_excerpt_id: String,
    val source_ref_id: String,
    val document_id: String,
    val chunk_id: String,
    val document_label: String,
    val heading_path: List<String>,
    val page_start: Int,
    val page_end: Int,
    val context_text: String,
    val highlight_start: Int,
    val highlight_end: Int,
    val excerpt_hash: String,
)

@Serializable
data class Illustration(
    val illustration_id: String,
    val source_ref_id: String,
    val asset_member: String,
    val mime_type: String,
    val sha256: String,
    val byte_size: Int,
    val width: Int,
    val height: Int,
    val page_number: Int,
    val caption: String,
    val alt_text: String,
)

@Serializable data class MindMapNode(val id: String, val label: String, val type: String, val atom_id: String?)
@Serializable data class MindMapEdge(@SerialName("from") val from: String, val to: String, val type: String)
@Serializable data class MindMap(val mind_map_id: String, val root_node_id: String, val nodes: List<MindMapNode>, val edges: List<MindMapEdge>)

@Serializable
data class FullLesson(
    val full_lesson_id: String,
    val module_id: String,
    val order_index: Int,
    val title: String,
    val objective: String,
    val atom_ids: List<String>,
    val estimated_minutes: Int,
)

@Serializable
data class PackManifest(
    val content_pack_id: String,
    val course_id: String,
    val version: String,
    val schema_version: String,
    val members: Map<String, String>,
    val ai_approved_count: Int,
    val repaired_count: Int,
    val rejected_count: Int,
)

data class ContentPack(
    val manifest: PackManifest,
    val course: Course,
    val modules: List<Module>,
    val fullLessons: List<FullLesson>,
    val atoms: List<KnowledgeAtom>,
    val lessons: List<Lesson>,
    val pairs: List<LearningPair>,
    val questions: List<Question>,
    val miniLabs: List<MiniLab>,
    val mindMaps: List<MindMap>,
    val sourceRefs: List<SourceRef>,
    val sourceExcerpts: List<SourceExcerpt>,
    val illustrations: List<Illustration> = emptyList(),
    private val illustrationAssets: Map<String, ByteArray> = emptyMap(),
) {
    private val moduleByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { modules.associateBy { it.module_id } }
    private val atomByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { atoms.associateBy { it.atom_id } }
    private val atomsByModuleIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { atoms.groupBy { it.module_id } }
    private val lessonByAtomIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { lessons.associateBy { it.atom_id } }
    private val lessonByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { lessons.associateBy { it.lesson_id } }
    private val pairByAtomIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { pairs.associateBy { it.atom_id } }
    private val pairByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { pairs.associateBy { it.pair_id } }
    private val questionByIdIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { questions.associateBy { it.question_id } }
    private val questionsByAtomIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { questions.groupBy { it.atom_id } }
    private val excerptBySourceIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { sourceExcerpts.groupBy { it.source_ref_id } }
    private val illustrationsBySourceIndex by lazy(LazyThreadSafetyMode.PUBLICATION) { illustrations.groupBy { it.source_ref_id } }
    private val orderedAtomsIndex by lazy(LazyThreadSafetyMode.PUBLICATION) {
        modules.sortedBy { it.order_index }.flatMap { module ->
            atomsByModuleIndex[module.module_id].orEmpty().sortedBy { it.order_index }
        }
    }

    fun moduleById(moduleId: String) = moduleByIdIndex[moduleId]
    fun atomById(atomId: String) = atomByIdIndex[atomId]
    fun atomsForModule(moduleId: String) = atomsByModuleIndex[moduleId].orEmpty()
    fun orderedAtoms(): List<KnowledgeAtom> = orderedAtomsIndex
    fun lessonForAtom(atomId: String) = lessonByAtomIndex[atomId]
    fun pairForAtom(atomId: String) = pairByAtomIndex[atomId]
    fun pairById(pairId: String) = pairByIdIndex[pairId]
    fun lessonById(lessonId: String) = lessonByIdIndex[lessonId]
    fun questionById(questionId: String) = questionByIdIndex[questionId]
    fun questionsForPair(pairId: String): List<Question> {
        val pair = pairById(pairId) ?: return emptyList()
        return pair.question_ids.mapNotNull(questionByIdIndex::get)
    }
    fun questionsForAtom(atomId: String) = questionsByAtomIndex[atomId].orEmpty()
    fun labForAtom(atomId: String) = miniLabs.firstOrNull { it.atom_id == atomId }
    fun excerptsFor(sourceIds: Collection<String>) = sourceIds.flatMap { excerptBySourceIndex[it].orEmpty() }
    fun illustrationsForLesson(lesson: Lesson): List<Illustration> {
        return lesson.illustration_source_ref_ids.flatMap { illustrationsBySourceIndex[it].orEmpty() }
    }
    fun bytesForIllustration(illustration: Illustration): ByteArray? = illustrationAssets[illustration.asset_member]
}
