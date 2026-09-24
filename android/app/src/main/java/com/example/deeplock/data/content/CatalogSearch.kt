package com.example.deeplock.data.content

import java.text.Normalizer
import java.util.Locale

internal fun normalizeCatalogSearch(value: String): String = Normalizer
    .normalize(value, Normalizer.Form.NFD)
    .replace("đ", "d", ignoreCase = true)
    .replace(Regex("\\p{M}+"), "")
    .lowercase(Locale.ROOT)
    .replace(Regex("\\s+"), " ")
    .trim()

fun ContentCatalog.searchLectures(query: String, categoryId: String? = null): List<CatalogLecture> {
    val needle = normalizeCatalogSearch(query)
    val packsByLecture = packs.groupBy { it.lecture_id }
    val atomsByPack = atoms.groupBy { it.pack_id }
    return lectures.asSequence()
        .filter { categoryId == null || it.category_id == categoryId }
        .filter { lecture ->
            if (needle.isBlank()) return@filter true
            val category = categoryById(lecture.category_id)
            val descriptors = packsByLecture[lecture.lecture_id].orEmpty()
            sequenceOf(
                category?.title,
                category?.description,
                category?.search_terms?.joinToString(" "),
                lecture.title,
                lecture.description,
                lecture.search_terms.joinToString(" "),
                descriptors.joinToString(" ") { it.title },
                descriptors.flatMap { atomsByPack[it.pack_id].orEmpty() }.joinToString(" ") { it.title },
            ).filterNotNull().any { needle in normalizeCatalogSearch(it) }
        }
        .sortedWith(compareBy<CatalogLecture>(
            { categoryById(it.category_id)?.order_index ?: Int.MAX_VALUE },
            { it.order_index },
            { it.lecture_id },
        ))
        .toList()
}
