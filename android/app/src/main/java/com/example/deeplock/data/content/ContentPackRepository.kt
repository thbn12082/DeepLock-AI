package com.example.deeplock.data.content

import android.content.Context
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.ByteArrayOutputStream
import java.io.FileNotFoundException
import java.io.InputStream
import java.security.MessageDigest
import java.util.LinkedHashMap
import java.util.zip.ZipInputStream
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

private const val REQUIRED_QUIZZES_PER_ATOM = 5

@Singleton
class ContentPackRepository @Inject constructor(@ApplicationContext private val context: Context) {
    private val mutex = Mutex()
    private val cache = PackLruCache<String, ContentPack>(MAX_CACHED_PACKS)
    @Volatile private var cachedCatalog: ContentCatalog? = null

    /** Backward-compatible entry point used by older callers and the existing default pack. */
    suspend fun load(): ContentPack {
        val catalog = catalog()
        return load(catalog.default_pack_id)
    }

    suspend fun catalog(): ContentCatalog = cachedCatalog ?: mutex.withLock {
        cachedCatalog ?: withContext(Dispatchers.IO) { readCatalog() }.also { result ->
            result.warmedPack?.let { cache.put(result.catalog.default_pack_id, it) }
            cachedCatalog = result.catalog
        }.catalog
    }

    suspend fun load(packId: String): ContentPack {
        cache[packId]?.let { return it }
        val catalog = catalog()
        val descriptor = requireNotNull(catalog.packById(packId)) { "Unknown content pack: $packId" }
        return mutex.withLock {
            cache[packId] ?: withContext(Dispatchers.IO) { readAndVerify(descriptor, catalog) }
                .also { cache.put(packId, it) }
        }
    }

    suspend fun packIdForAtom(atomId: String): String? = catalog().packIdForAtom(atomId)
    suspend fun packIdForPair(pairId: String): String? = catalog().packIdForPair(pairId)
    suspend fun packIdForQuestion(questionId: String): String? = catalog().packIdForQuestion(questionId)
    suspend fun loadForAtom(atomId: String): ContentPack = load(
        requireNotNull(packIdForAtom(atomId)) { "Unknown atom: $atomId" },
    )
    suspend fun loadForPair(pairId: String): ContentPack = load(
        requireNotNull(packIdForPair(pairId)) { "Unknown pair: $pairId" },
    )
    suspend fun loadForQuestion(questionId: String): ContentPack = load(
        requireNotNull(packIdForQuestion(questionId)) { "Unknown question: $questionId" },
    )

    private data class CatalogLoad(val catalog: ContentCatalog, val warmedPack: ContentPack?)

    private fun readCatalog(): CatalogLoad {
        val decoded = try {
            context.assets.open(CATALOG_ASSET).use(ContentCatalogCodec::decode)
        } catch (_: FileNotFoundException) {
            null
        }
        if (decoded == null) {
            val pack = context.assets.open(PACK_ASSET).use(ContentPackCodec::decode)
            return CatalogLoad(ContentCatalogCodec.legacy(pack, PACK_ASSET), pack)
        }
        if (decoded.atoms.isNotEmpty()) return CatalogLoad(decoded, null)
        require(decoded.packs.size == 1) {
            "Multi-pack catalog must include the lightweight atom ownership index"
        }
        val descriptor = decoded.packs.single()
        val pack = readAndVerify(descriptor)
        return CatalogLoad(ContentCatalogCodec.deriveAtoms(decoded, descriptor.pack_id, pack), pack)
    }

    private fun readAndVerify(descriptor: CatalogPack, catalog: ContentCatalog? = null): ContentPack {
        descriptor.asset_sha256?.let { expected ->
            // ZipInputStream stops at the central directory, so hashing the decoder stream can
            // cover only a prefix. Hash the complete asset before opening it as a dlpack.
            val actual = context.assets.open(descriptor.asset_path).use(::sha256)
            require(actual == expected) { "Catalog asset checksum mismatch: ${descriptor.pack_id}" }
        }
        val pack = context.assets.open(descriptor.asset_path).use(ContentPackCodec::decode)
        require(pack.manifest.content_pack_id == descriptor.pack_id) { "Catalog/pack id mismatch" }
        require(pack.manifest.course_id == descriptor.course_id && pack.course.course_id == descriptor.course_id) {
            "Catalog/pack course mismatch"
        }
        catalog?.takeIf { it.atoms.isNotEmpty() }?.let { ContentCatalogCodec.validatePackOwnership(it, descriptor, pack) }
        return pack
    }

    private fun sha256(input: InputStream): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
        return "sha256:" + digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }

    companion object {
        const val CATALOG_ASSET = "content/catalog.json"
        const val PACK_ASSET = "content/default.dlpack"
        private const val MAX_CACHED_PACKS = 2
    }
}

internal class PackLruCache<K, V>(private val maxEntries: Int) {
    init { require(maxEntries > 0) }
    private val values = LinkedHashMap<K, V>(maxEntries, 0.75f, true)
    @Synchronized operator fun get(key: K): V? = values[key]
    @Synchronized fun put(key: K, value: V) {
        values[key] = value
        while (values.size > maxEntries) values.remove(values.entries.first().key)
    }
    @Synchronized fun keys(): List<K> = values.keys.toList()
}

object ContentCatalogCodec {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = true }
    private val safeAsset = Regex("content/[A-Za-z0-9_./-]+\\.dlpack")
    private val sha256Pattern = Regex("sha256:[0-9a-f]{64}")

    fun decode(input: InputStream): ContentCatalog {
        val payload = readBounded(input, MAX_CATALOG_BYTES)
        return validate(json.decodeFromString(payload.decodeToString()))
    }

    fun validate(catalog: ContentCatalog): ContentCatalog {
        require(catalog.schema_version == "1.0") { "Unsupported catalog schema" }
        require(catalog.catalog_id.isNotBlank() && catalog.title.isNotBlank() && catalog.language.isNotBlank()) {
            "Catalog identity is required"
        }
        requireUnique(catalog.categories.map { it.category_id }, "category")
        requireUnique(catalog.lectures.map { it.lecture_id }, "lecture")
        requireUnique(catalog.packs.map { it.pack_id }, "pack")
        require(catalog.categories.isNotEmpty() && catalog.lectures.isNotEmpty() && catalog.packs.isNotEmpty()) {
            "Catalog hierarchy is empty"
        }
        val categoryIds = catalog.categories.mapTo(mutableSetOf()) { it.category_id }
        val lectureIds = catalog.lectures.mapTo(mutableSetOf()) { it.lecture_id }
        val packIds = catalog.packs.mapTo(mutableSetOf()) { it.pack_id }
        require(catalog.default_pack_id in packIds) { "Default pack is missing" }
        require(catalog.lectures.all {
            it.category_id in categoryIds && it.order_index >= 0 && it.title.isNotBlank()
        }) {
            "Lecture has invalid category/order"
        }
        require(catalog.categories.all { it.order_index >= 0 && it.title.isNotBlank() }) { "Category order is invalid" }
        require(catalog.packs.all { descriptor ->
            descriptor.lecture_id in lectureIds && descriptor.order_index >= 0 &&
                descriptor.title.isNotBlank() && descriptor.course_id.isNotBlank() &&
                descriptor.asset_path.matches(safeAsset) && ".." !in descriptor.asset_path &&
                (descriptor.asset_sha256 == null || descriptor.asset_sha256.matches(sha256Pattern))
        }) { "Pack descriptor is invalid" }
        requireUnique(catalog.packs.map { it.asset_path }, "pack asset")
        require(categoryIds.all { id -> catalog.lectures.any { it.category_id == id } }) { "Catalog category has no lectures" }
        require(lectureIds.all { id -> catalog.packs.any { it.lecture_id == id } }) { "Catalog lecture has no packs" }
        if (catalog.atoms.isNotEmpty()) {
            requireUnique(catalog.atoms.map { it.atom_id }, "atom")
            requireUnique(catalog.atoms.map { it.pair_id }, "pair")
            requireUnique(catalog.atoms.map { it.lesson_id }, "lesson")
            requireUnique(catalog.atoms.flatMap { it.question_ids }, "question")
            requireUnique(catalog.atoms.map { it.order_index.toString() }, "atom order")
            val atomIds = catalog.atoms.mapTo(mutableSetOf()) { it.atom_id }
            require(catalog.atoms.all { atom ->
                atom.pack_id in packIds && atom.module_id.isNotBlank() && atom.title.isNotBlank() &&
                    atom.order_index >= 0 && atom.question_ids.size == REQUIRED_QUIZZES_PER_ATOM &&
                    atom.question_ids.distinct().size == atom.question_ids.size &&
                    atom.prerequisite_ids.distinct().size == atom.prerequisite_ids.size &&
                    atom.atom_id !in atom.prerequisite_ids && atom.prerequisite_ids.all { it in atomIds }
            }) { "Catalog atom ownership/prerequisite is invalid" }
            require(packIds.all { id -> catalog.atoms.any { it.pack_id == id } }) { "Catalog pack has no atoms" }
            validateAcyclicPrerequisites(catalog.atoms)
        }
        return catalog
    }

    fun validatePackOwnership(catalog: ContentCatalog, descriptor: CatalogPack, pack: ContentPack) {
        val summaries = catalog.atomsForPack(descriptor.pack_id)
        require(summaries.isNotEmpty() && summaries.size == pack.atoms.size) {
            "Catalog/pack atom set mismatch: ${descriptor.pack_id}"
        }
        val summaryIds = summaries.mapTo(mutableSetOf()) { it.atom_id }
        require(summaryIds == pack.atoms.mapTo(mutableSetOf()) { it.atom_id }) {
            "Catalog/pack atom IDs mismatch: ${descriptor.pack_id}"
        }
        summaries.forEach { summary ->
            val atom = requireNotNull(pack.atomById(summary.atom_id))
            val pair = requireNotNull(pack.pairForAtom(summary.atom_id))
            val lesson = requireNotNull(pack.lessonForAtom(summary.atom_id))
            require(
                atom.module_id == summary.module_id && atom.title == summary.title &&
                    atom.prerequisite_ids == summary.prerequisite_ids &&
                    pair.pair_id == summary.pair_id && pair.micro_lesson_id == summary.lesson_id &&
                    pair.question_ids == summary.question_ids && lesson.lesson_id == summary.lesson_id,
            ) { "Catalog/pack owner metadata mismatch: ${summary.atom_id}" }
        }
    }

    fun deriveAtoms(catalog: ContentCatalog, packId: String, pack: ContentPack): ContentCatalog {
        require(catalog.atoms.isEmpty()) { "Catalog already has atom ownership" }
        require(catalog.packById(packId) != null) { "Cannot derive atoms for unknown pack" }
        val atoms = pack.orderedAtoms().mapIndexed { index, atom ->
            val pair = requireNotNull(pack.pairForAtom(atom.atom_id))
            val lesson = requireNotNull(pack.lessonForAtom(atom.atom_id))
            CatalogAtom(
                pack_id = packId,
                atom_id = atom.atom_id,
                pair_id = pair.pair_id,
                lesson_id = lesson.lesson_id,
                module_id = atom.module_id,
                title = atom.title,
                order_index = index,
                prerequisite_ids = atom.prerequisite_ids,
                question_ids = pair.question_ids,
            )
        }
        return validate(catalog.copy(atoms = atoms))
    }

    fun legacy(pack: ContentPack, assetPath: String): ContentCatalog {
        val categoryId = "legacy_${pack.course.course_id}"
        val lectureId = "lecture_${pack.course.course_id}"
        val catalog = ContentCatalog(
            schema_version = "1.0",
            catalog_id = "catalog_${pack.course.course_id}",
            title = pack.course.title,
            language = pack.course.language,
            default_pack_id = pack.manifest.content_pack_id,
            categories = listOf(CatalogCategory(categoryId, pack.course.title, pack.course.description, 0)),
            lectures = listOf(CatalogLecture(lectureId, categoryId, pack.course.title, pack.course.description, 0)),
            packs = listOf(CatalogPack(
                pack_id = pack.manifest.content_pack_id,
                lecture_id = lectureId,
                asset_path = assetPath,
                title = pack.course.title,
                order_index = 0,
                course_id = pack.course.course_id,
            )),
        )
        return deriveAtoms(catalog, pack.manifest.content_pack_id, pack)
    }

    private fun requireUnique(values: List<String>, label: String) {
        require(values.all { it.isNotBlank() } && values.distinct().size == values.size) { "Duplicate/blank $label id" }
    }

    private fun validateAcyclicPrerequisites(atoms: List<CatalogAtom>) {
        val prerequisites = atoms.associate { it.atom_id to it.prerequisite_ids }
        val visiting = mutableSetOf<String>()
        val visited = mutableSetOf<String>()
        fun visit(id: String) {
            if (id in visited) return
            require(visiting.add(id)) { "Cyclic catalog prerequisite graph" }
            prerequisites.getValue(id).forEach(::visit)
            visiting.remove(id)
            visited.add(id)
        }
        prerequisites.keys.forEach(::visit)
    }

    private fun readBounded(input: InputStream, limit: Int): ByteArray {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var total = 0
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            total += count
            require(total <= limit) { "Content catalog is too large" }
            output.write(buffer, 0, count)
        }
        return output.toByteArray()
    }

    private const val MAX_CATALOG_BYTES = 32 * 1024 * 1024
}

object ContentPackCodec {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = true }
    private val safeMember = Regex("[A-Za-z0-9_.-]+")
    private val illustrationMember = Regex("[A-Za-z0-9][A-Za-z0-9_.-]*\\.(?:png|webp)")
    private val sha256Pattern = Regex("sha256:[0-9a-f]{64}")
    private val illustrationMimeTypes = setOf("image/png", "image/webp")

    fun decode(input: InputStream): ContentPack {
        val members = mutableMapOf<String, ByteArray>()
        var totalBytes = 0L
        var entryCount = 0
        ZipInputStream(input.buffered()).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                require(!entry.isDirectory && entry.name.matches(safeMember)) { "Unsafe content pack member" }
                require(entry.name !in members) { "Duplicate content pack member: ${entry.name}" }
                require(++entryCount <= MAX_PACK_ENTRIES) { "Too many content pack members" }
                val payload = readBounded(zip, MAX_MEMBER_BYTES)
                totalBytes += payload.size
                require(totalBytes <= MAX_PACK_BYTES) { "Content pack is too large" }
                members[entry.name] = payload
                zip.closeEntry()
            }
        }
        val manifest = decode<PackManifest>(members, "manifest.json")
        require(manifest.schema_version == "1.0") { "Unsupported pack schema" }
        require(manifest.rejected_count == 0 && manifest.ai_approved_count > 0) { "Pack is not AI-approved" }
        require(manifest.members.keys == members.keys - "manifest.json") { "Manifest member set mismatch" }
        manifest.members.forEach { (name, expected) ->
            val actual = "sha256:" + MessageDigest.getInstance("SHA-256")
                .digest(requireNotNull(members[name])).joinToString("") { "%02x".format(it.toInt() and 0xff) }
            require(actual == expected) { "Content pack checksum mismatch: $name" }
        }
        val illustrations = decodeOptional<List<Illustration>>(members, ILLUSTRATIONS_MEMBER).orEmpty()
        val illustrationMembers = illustrations.map { it.asset_member }.toSet()
        val packagedImages = members.keys.filter { it.endsWith(".png") || it.endsWith(".webp") }.toSet()
        require(packagedImages == illustrationMembers) { "Illustration asset member set mismatch" }
        val illustrationAssets = illustrationMembers.associateWith { name ->
            requireNotNull(members[name]) { "Missing illustration asset: $name" }
        }
        val pack = ContentPack(
            manifest = manifest,
            course = decode(members, "course.json"),
            modules = decode(members, "modules.json"),
            fullLessons = decode(members, "full_lessons.json"),
            atoms = decode(members, "atoms.json"),
            lessons = decode(members, "lessons.json"),
            pairs = decode(members, "learning_pairs.json"),
            questions = decode(members, "questions.json"),
            miniLabs = decode(members, "mini_labs.json"),
            mindMaps = decode(members, "mindmaps.json"),
            sourceRefs = decode(members, "source_refs.json"),
            sourceExcerpts = decode(members, "source_excerpts.json"),
            illustrations = illustrations,
            illustrationAssets = illustrationAssets,
        )
        require(pack.pairs.isNotEmpty() && pack.pairs.all { it.ai_review_status == "AI_APPROVED" }) { "LearningPair review gate failed" }
        require(pack.miniLabs.all { it.ai_review_status == "AI_APPROVED" }) { "Mini Lab review gate failed" }
        validateRuntimeIntegrity(pack)
        return pack
    }

    private fun validateRuntimeIntegrity(pack: ContentPack) {
        require(pack.atoms.map { it.atom_id }.distinct().size == pack.atoms.size) { "Duplicate atom id" }
        require(pack.lessons.map { it.lesson_id }.distinct().size == pack.lessons.size) { "Duplicate lesson id" }
        require(pack.pairs.map { it.pair_id }.distinct().size == pack.pairs.size) { "Duplicate pair id" }
        require(pack.questions.map { it.question_id }.distinct().size == pack.questions.size) { "Duplicate question id" }
        val lessons = pack.lessons.associateBy { it.lesson_id }
        val questions = pack.questions.associateBy { it.question_id }
        val owners = mutableMapOf<String, String>()
        pack.pairs.forEach { pair ->
            val lesson = requireNotNull(lessons[pair.micro_lesson_id]) { "Pair lesson missing: ${pair.pair_id}" }
            require(pair.atom_id == lesson.atom_id && pair.pair_id == lesson.pair_id) { "Pair/lesson atom mismatch" }
            require(
                pair.question_ids.size == REQUIRED_QUIZZES_PER_ATOM &&
                    pair.question_ids.distinct().size == pair.question_ids.size,
            ) { "Pair needs exactly $REQUIRED_QUIZZES_PER_ATOM distinct questions" }
            pair.question_ids.forEach { id ->
                val question = requireNotNull(questions[id]) { "Pair question missing: $id" }
                require(question.pair_id == pair.pair_id && question.atom_id == pair.atom_id) { "Pair/question mismatch: $id" }
                require(owners.put(id, pair.pair_id) == null) { "Question belongs to more than one pair: $id" }
            }
        }
        require(pack.questions.all { owners[it.question_id] == it.pair_id }) { "Question is outside its declared pair" }

        val sourceIds = pack.sourceRefs.map { it.source_ref_id }.toSet()
        val sourcesById = pack.sourceRefs.associateBy { it.source_ref_id }
        pack.sourceExcerpts.forEach { excerpt ->
            require(excerpt.source_ref_id in sourceIds) { "Excerpt source missing" }
            val digest = "sha256:" + MessageDigest.getInstance("SHA-256")
                .digest(excerpt.context_text.encodeToByteArray())
                .joinToString("") { "%02x".format(it.toInt() and 0xff) }
            require(digest == excerpt.excerpt_hash) { "Excerpt hash mismatch" }
        }
        require(pack.lessons.all { sourceIds.containsAll(it.source_ref_ids) }) { "Lesson source missing" }
        require(pack.questions.all { sourceIds.containsAll(it.source_ref_ids) }) { "Question source missing" }

        require(pack.illustrations.map { it.illustration_id }.distinct().size == pack.illustrations.size) {
            "Duplicate illustration id"
        }
        require(pack.illustrations.map { it.asset_member }.distinct().size == pack.illustrations.size) {
            "Duplicate illustration asset member"
        }
        pack.illustrations.forEach { illustration ->
            require(illustration.source_ref_id in sourceIds) { "Illustration source missing: ${illustration.illustration_id}" }
            require(illustration.asset_member.length <= 180 && illustration.asset_member.matches(illustrationMember)) {
                "Unsafe illustration asset member"
            }
            require(illustration.mime_type in illustrationMimeTypes) { "Unsupported illustration MIME type" }
            require(
                (illustration.mime_type == "image/png" && illustration.asset_member.endsWith(".png")) ||
                    (illustration.mime_type == "image/webp" && illustration.asset_member.endsWith(".webp")),
            ) { "Illustration extension/MIME mismatch" }
            require(illustration.sha256.matches(sha256Pattern)) { "Invalid illustration checksum" }
            require(illustration.width in 1..MAX_IMAGE_DIMENSION && illustration.height in 1..MAX_IMAGE_DIMENSION) {
                "Invalid illustration dimensions"
            }
            require(illustration.width.toLong() * illustration.height <= MAX_IMAGE_PIXELS) {
                "Illustration has too many pixels"
            }
            require(
                illustration.caption.isNotBlank() && illustration.caption.length <= 500 &&
                    illustration.alt_text.isNotBlank() && illustration.alt_text.length <= 500,
            ) {
                "Illustration caption and alt text are required"
            }
            val asset = requireNotNull(pack.bytesForIllustration(illustration)) { "Illustration asset missing" }
            require(asset.size in 1..MAX_IMAGE_BYTES) { "Illustration asset is too large" }
            require(illustration.byte_size == asset.size) { "Illustration byte size does not match asset" }
            require(sha256(asset) == illustration.sha256) { "Illustration checksum mismatch" }
            require(imageDimensions(asset, illustration.mime_type) == (illustration.width to illustration.height)) {
                "Illustration dimensions do not match asset"
            }
            val source = requireNotNull(sourcesById[illustration.source_ref_id])
            require(illustration.page_number in source.page_start..source.page_end) {
                "Illustration page is outside source range"
            }
        }
        val illustrationSourceIds = pack.illustrations.map { it.source_ref_id }.toSet()
        pack.lessons.forEach { lesson ->
            require(lesson.illustration_source_ref_ids.distinct().size == lesson.illustration_source_ref_ids.size) {
                "Duplicate lesson illustration source"
            }
            require(lesson.source_ref_ids.containsAll(lesson.illustration_source_ref_ids)) {
                "Lesson illustration is outside lesson sources"
            }
            require(illustrationSourceIds.containsAll(lesson.illustration_source_ref_ids)) {
                "Lesson illustration source has no asset"
            }
        }
        val usedIllustrationSources = pack.lessons.flatMap { it.illustration_source_ref_ids }.toSet()
        require(pack.illustrations.all { it.source_ref_id in usedIllustrationSources }) {
            "Illustration is not used by a lesson"
        }

        val atomIds = pack.atoms.map { it.atom_id }.toSet()
        val visiting = mutableSetOf<String>()
        val visited = mutableSetOf<String>()
        val prerequisites = pack.atoms.associate { it.atom_id to it.prerequisite_ids }
        fun visit(id: String) {
            require(id in atomIds) { "Unknown prerequisite atom: $id" }
            if (id in visited) return
            require(visiting.add(id)) { "Cyclic prerequisite graph" }
            prerequisites[id].orEmpty().forEach(::visit)
            visiting.remove(id)
            visited.add(id)
        }
        atomIds.forEach(::visit)
    }

    private inline fun <reified T> decode(members: Map<String, ByteArray>, name: String): T =
        json.decodeFromString(requireNotNull(members[name]) { "Missing $name" }.decodeToString())

    private inline fun <reified T> decodeOptional(members: Map<String, ByteArray>, name: String): T? =
        members[name]?.let { json.decodeFromString<T>(it.decodeToString()) }

    private fun readBounded(input: InputStream, limit: Int): ByteArray {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var total = 0
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            total += count
            require(total <= limit) { "Content pack member is too large" }
            output.write(buffer, 0, count)
        }
        return output.toByteArray()
    }

    private fun sha256(payload: ByteArray): String = "sha256:" + MessageDigest.getInstance("SHA-256")
        .digest(payload).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun imageDimensions(payload: ByteArray, mimeType: String): Pair<Int, Int>? = when (mimeType) {
        "image/png" -> pngDimensions(payload)
        "image/webp" -> webpDimensions(payload)
        else -> null
    }

    private fun pngDimensions(payload: ByteArray): Pair<Int, Int>? {
        val signature = byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
        if (payload.size < 24 || !payload.copyOfRange(0, 8).contentEquals(signature)) return null
        if (payload.copyOfRange(12, 16).decodeToString() != "IHDR") return null
        val width = readBigEndianInt(payload, 16)
        val height = readBigEndianInt(payload, 20)
        return if (width > 0 && height > 0) width to height else null
    }

    private fun webpDimensions(payload: ByteArray): Pair<Int, Int>? {
        if (payload.size < 30 || ascii(payload, 0, 4) != "RIFF" || ascii(payload, 8, 4) != "WEBP") return null
        if (readLittleEndianInt(payload, 4).toLong() + 8 != payload.size.toLong()) return null
        var offset = 12
        while (offset + 8 <= payload.size) {
            val type = ascii(payload, offset, 4)
            val chunkSize = readLittleEndianInt(payload, offset + 4)
            if (chunkSize < 0) return null
            val data = offset + 8
            val end = data.toLong() + chunkSize
            if (end > payload.size) return null
            val dimensions = when (type) {
                "VP8X" -> if (chunkSize >= 10) {
                    (readLittleEndian24(payload, data + 4) + 1) to (readLittleEndian24(payload, data + 7) + 1)
                } else null
                "VP8L" -> if (chunkSize >= 5 && unsigned(payload[data]) == 0x2f) {
                    val b1 = unsigned(payload[data + 1])
                    val b2 = unsigned(payload[data + 2])
                    val b3 = unsigned(payload[data + 3])
                    val b4 = unsigned(payload[data + 4])
                    (1 + b1 + ((b2 and 0x3f) shl 8)) to
                        (1 + ((b2 and 0xc0) shr 6) + (b3 shl 2) + ((b4 and 0x0f) shl 10))
                } else null
                "VP8 " -> if (
                    chunkSize >= 10 && unsigned(payload[data + 3]) == 0x9d &&
                    unsigned(payload[data + 4]) == 0x01 && unsigned(payload[data + 5]) == 0x2a
                ) {
                    (readLittleEndian16(payload, data + 6) and 0x3fff) to
                        (readLittleEndian16(payload, data + 8) and 0x3fff)
                } else null
                else -> null
            }
            if (dimensions != null && dimensions.first > 0 && dimensions.second > 0) return dimensions
            offset = (end + (chunkSize and 1)).toInt()
        }
        return null
    }

    private fun ascii(payload: ByteArray, offset: Int, length: Int): String =
        payload.copyOfRange(offset, offset + length).decodeToString()

    private fun unsigned(value: Byte): Int = value.toInt() and 0xff
    private fun readLittleEndian16(payload: ByteArray, offset: Int): Int =
        unsigned(payload[offset]) or (unsigned(payload[offset + 1]) shl 8)
    private fun readLittleEndian24(payload: ByteArray, offset: Int): Int =
        readLittleEndian16(payload, offset) or (unsigned(payload[offset + 2]) shl 16)
    private fun readLittleEndianInt(payload: ByteArray, offset: Int): Int =
        unsigned(payload[offset]) or (unsigned(payload[offset + 1]) shl 8) or
            (unsigned(payload[offset + 2]) shl 16) or (unsigned(payload[offset + 3]) shl 24)
    private fun readBigEndianInt(payload: ByteArray, offset: Int): Int =
        (unsigned(payload[offset]) shl 24) or (unsigned(payload[offset + 1]) shl 16) or
            (unsigned(payload[offset + 2]) shl 8) or unsigned(payload[offset + 3])

    private const val ILLUSTRATIONS_MEMBER = "illustrations.json"
    private const val MAX_PACK_ENTRIES = 512
    private const val MAX_MEMBER_BYTES = 32 * 1024 * 1024
    private const val MAX_PACK_BYTES = 128L * 1024 * 1024
    private const val MAX_IMAGE_BYTES = 16 * 1024 * 1024
    private const val MAX_IMAGE_DIMENSION = 8_192
    private const val MAX_IMAGE_PIXELS = 16_777_216L
}
