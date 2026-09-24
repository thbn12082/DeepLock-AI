package com.example.deeplock.data.content

import com.google.common.truth.Truth.assertThat
import java.io.ByteArrayInputStream
import java.io.File
import org.junit.Assert.assertThrows
import org.junit.Test

class ContentCatalogCodecTest {
    @Test fun bundledCatalogResolvesAndValidatesItsDefaultPack() {
        val raw = File("src/main/assets/content/catalog.json").inputStream().use(ContentCatalogCodec::decode)
        val descriptor = requireNotNull(raw.packById(raw.default_pack_id))
        val pack = File("src/main/assets/${descriptor.asset_path}").inputStream().use(ContentPackCodec::decode)
        val catalog = if (raw.atoms.isEmpty()) {
            ContentCatalogCodec.deriveAtoms(raw, raw.default_pack_id, pack)
        } else {
            raw
        }

        assertThat(catalog.default_pack_id).isEqualTo(pack.manifest.content_pack_id)
        assertThat(catalog.atomsForPack(raw.default_pack_id)).hasSize(pack.atoms.size)
        assertThat(catalog.atomsForPack(raw.default_pack_id).map { it.atom_id })
            .containsExactlyElementsIn(pack.orderedAtoms().map { it.atom_id }).inOrder()
        pack.pairs.forEach { pair -> assertThat(catalog.packIdForPair(pair.pair_id)).isEqualTo(raw.default_pack_id) }
        pack.questions.forEach { question -> assertThat(catalog.packIdForQuestion(question.question_id)).isEqualTo(raw.default_pack_id) }
        ContentCatalogCodec.validatePackOwnership(catalog, descriptor, pack)
        val ownedAtomIndex = catalog.atoms.indexOfFirst { it.pack_id == raw.default_pack_id }
        assertThat(ownedAtomIndex).isAtLeast(0)
        val corruptedAtoms = catalog.atoms.mapIndexed { index, atom ->
            if (index == ownedAtomIndex) {
                atom.copy(title = "Not the packaged atom title")
            } else {
                atom
            }
        }
        assertThrows(IllegalArgumentException::class.java) {
            ContentCatalogCodec.validatePackOwnership(
                catalog.copy(atoms = corruptedAtoms),
                descriptor,
                pack,
            )
        }
    }

    @Test fun mlopsSearchMatchesCategoryAndIsAccentInsensitive() {
        val catalog = syntheticCatalog()

        assertThat(catalog.searchLectures("MLOps").map { it.lecture_id }).containsExactly("lecture_monitoring")
        assertThat(catalog.searchLectures("giam sat mo hinh").map { it.lecture_id }).containsExactly("lecture_monitoring")
        assertThat(catalog.searchLectures("LLMOps").map { it.lecture_id }).containsExactly("lecture_monitoring")
        assertThat(catalog.searchLectures("MLOps", categoryId = "category_dl")).isEmpty()
    }

    @Test fun catalogRejectsDuplicateQuestionOwnershipAndUnsafeAssets() {
        val catalog = syntheticCatalog()
        val duplicateQuestion = catalog.atoms.last().copy(question_ids = catalog.atoms.first().question_ids)
        assertThrows(IllegalArgumentException::class.java) {
            ContentCatalogCodec.validate(catalog.copy(atoms = catalog.atoms.dropLast(1) + duplicateQuestion))
        }
        val unsafe = catalog.packs.first().copy(asset_path = "content/../outside.dlpack")
        assertThrows(IllegalArgumentException::class.java) {
            ContentCatalogCodec.validate(catalog.copy(packs = listOf(unsafe) + catalog.packs.drop(1)))
        }
        val duplicateOrder = catalog.atoms.last().copy(order_index = catalog.atoms.first().order_index)
        assertThrows(IllegalArgumentException::class.java) {
            ContentCatalogCodec.validate(catalog.copy(atoms = catalog.atoms.dropLast(1) + duplicateOrder))
        }
        val fewerThanFiveQuizzes = catalog.atoms.first().copy(
            question_ids = catalog.atoms.first().question_ids.dropLast(1),
        )
        assertThrows(IllegalArgumentException::class.java) {
            ContentCatalogCodec.validate(
                catalog.copy(atoms = listOf(fewerThanFiveQuizzes) + catalog.atoms.drop(1)),
            )
        }
        val cycle = catalog.atoms.mapIndexed { index, atom ->
            when (index) {
                0 -> atom.copy(prerequisite_ids = listOf(catalog.atoms[1].atom_id))
                1 -> atom.copy(prerequisite_ids = listOf(catalog.atoms[0].atom_id))
                else -> atom
            }
        }
        assertThrows(IllegalArgumentException::class.java) {
            ContentCatalogCodec.validate(catalog.copy(atoms = cycle))
        }
    }

    @Test fun lruKeepsOnlyTheTwoMostRecentlyUsedPacks() {
        val cache = PackLruCache<String, String>(2)
        cache.put("a", "A")
        cache.put("b", "B")
        assertThat(cache["a"]).isEqualTo("A")
        cache.put("c", "C")

        assertThat(cache.keys()).containsExactly("a", "c").inOrder()
        assertThat(cache["b"]).isNull()
    }

    @Test fun catalogJsonRoundTripShapeRequiresGlobalOwnershipIndexForMultiplePacks() {
        val catalog = syntheticCatalog()
        val json = """
            {
              "schema_version":"1.0","catalog_id":"catalog","title":"All lectures","language":"vi",
              "default_pack_id":"pack_mlops",
              "categories":[{"category_id":"category_mlops","title":"MLOps","order_index":0}],
              "lectures":[{"lecture_id":"lecture_monitoring","category_id":"category_mlops","title":"Monitoring","order_index":0}],
              "packs":[{"pack_id":"pack_mlops","lecture_id":"lecture_monitoring","asset_path":"content/packs/mlops.dlpack","title":"MLOps","order_index":0,"course_id":"course_mlops"}],
              "atoms":[{"pack_id":"pack_mlops","atom_id":"atom_monitor","pair_id":"pair_monitor","lesson_id":"lesson_monitor","module_id":"module_monitor","title":"Monitoring","order_index":0,"prerequisite_ids":[],"question_ids":["q1","q2","q3","q4","q5"]}]
            }
        """.trimIndent()
        val decoded = ContentCatalogCodec.decode(ByteArrayInputStream(json.encodeToByteArray()))

        assertThat(decoded.packIdForAtom("atom_monitor")).isEqualTo("pack_mlops")
        assertThat(decoded.packIdForQuestion("q2")).isEqualTo("pack_mlops")
        assertThat(catalog.atoms.map { it.order_index }).isInOrder()
    }

    private fun syntheticCatalog(): ContentCatalog = ContentCatalogCodec.validate(ContentCatalog(
        schema_version = "1.0",
        catalog_id = "catalog_all",
        title = "All lectures",
        language = "vi",
        default_pack_id = "pack_mlops",
        categories = listOf(
            CatalogCategory("category_mlops", "MLOps", "Triển khai và giám sát", 0, listOf("LLMOps")),
            CatalogCategory("category_dl", "Deep Learning", order_index = 1),
        ),
        lectures = listOf(
            CatalogLecture("lecture_monitoring", "category_mlops", "Giám sát mô hình", "Monitoring production", 0),
            CatalogLecture("lecture_cnn", "category_dl", "CNN", order_index = 0),
        ),
        packs = listOf(
            CatalogPack("pack_mlops", "lecture_monitoring", "content/packs/mlops.dlpack", "MLOps", 0, "course_mlops"),
            CatalogPack("pack_cnn", "lecture_cnn", "content/packs/cnn.dlpack", "CNN", 0, "course_cnn"),
        ),
        atoms = listOf(
            CatalogAtom("pack_mlops", "atom_monitor", "pair_monitor", "lesson_monitor", "module_monitor", "Model monitoring", 0, emptyList(), listOf("qm1", "qm2", "qm3", "qm4", "qm5")),
            CatalogAtom("pack_cnn", "atom_cnn", "pair_cnn", "lesson_cnn", "module_cnn", "Convolution", 1, emptyList(), listOf("qc1", "qc2", "qc3", "qc4", "qc5")),
        ),
    ))
}
