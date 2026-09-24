package com.example.deeplock.data.content

import com.google.common.truth.Truth.assertThat
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.Base64
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertThrows
import org.junit.Test

class ContentPackCodecTest {
    @Test fun bundledPackHashesAndApprovalGateAreValid() {
        val asset = File("src/main/assets/content/default.dlpack")
        assertThat(asset.isFile).isTrue()
        val pack = asset.inputStream().use(ContentPackCodec::decode)
        assertThat(pack.manifest.ai_approved_count).isGreaterThan(0)
        assertThat(pack.questions.size).isEqualTo(pack.pairs.size * 5)
        assertThat(pack.pairs).isNotEmpty()
        assertThat(pack.pairs.all { it.ai_review_status == "AI_APPROVED" }).isTrue()
        assertThat(pack.miniLabs.all { it.ai_review_status == "AI_APPROVED" }).isTrue()
    }

    @Test fun illustrationMetadataAndBytesAreVerifiedAndExposed() {
        val image = onePixelPng()
        val pack = ContentPackCodec.decode(ByteArrayInputStream(packWithIllustration(image)))

        val illustration = pack.illustrations.single()
        val lesson = pack.lessons.first()
        assertThat(pack.illustrationsForLesson(lesson)).containsExactly(illustration)
        assertThat(pack.bytesForIllustration(illustration)).isEqualTo(image)
        assertThat(illustration.mime_type).isEqualTo("image/png")
        assertThat(illustration.byte_size).isEqualTo(image.size)
        assertThat(illustration.width).isEqualTo(1)
        assertThat(illustration.height).isEqualTo(1)
    }

    @Test fun illustrationMetadataHashMismatchIsRejected() {
        val malformed = packWithIllustration(
            image = onePixelPng(),
            overrides = mapOf("sha256" to JsonPrimitive("sha256:" + "0".repeat(64))),
        )

        val error = assertThrows(IllegalArgumentException::class.java) {
            ContentPackCodec.decode(ByteArrayInputStream(malformed))
        }
        assertThat(error).hasMessageThat().contains("Illustration checksum mismatch")
    }

    @Test fun webpIllustrationHeaderIsSupported() {
        val image = onePixelWebpHeader()
        val pack = ContentPackCodec.decode(
            ByteArrayInputStream(
                packWithIllustration(
                    image = image,
                    mimeType = "image/webp",
                    assetName = "illustration_codec_test.webp",
                ),
            ),
        )

        val illustration = pack.illustrations.single()
        assertThat(illustration.mime_type).isEqualTo("image/webp")
        assertThat(pack.bytesForIllustration(illustration)).isEqualTo(image)
    }

    @Test fun lessonCannotReferenceIllustrationOutsideItsSources() {
        val malformed = packWithIllustration(
            image = onePixelPng(),
            lessonIllustrationSource = "src_not_owned_by_lesson",
        )

        val error = assertThrows(IllegalArgumentException::class.java) {
            ContentPackCodec.decode(ByteArrayInputStream(malformed))
        }
        assertThat(error).hasMessageThat().contains("outside lesson sources")
    }

    private fun packWithIllustration(
        image: ByteArray,
        overrides: Map<String, JsonElement> = emptyMap(),
        lessonIllustrationSource: String? = null,
        mimeType: String = "image/png",
        assetName: String = "illustration_codec_test.png",
    ): ByteArray {
        val members = readBundledMembers()
        members.keys.filter { it.endsWith(".png") || it.endsWith(".webp") }.forEach(members::remove)
        members.remove("illustrations.json")
        members.remove("manifest.json")

        val lessons = Json.parseToJsonElement(requireNotNull(members["lessons.json"]).decodeToString()).jsonArray
        val firstLesson = lessons.first().jsonObject
        val ownedSource = firstLesson.getValue("source_ref_ids").jsonArray.first().jsonPrimitive.content
        val linkedSource = lessonIllustrationSource ?: ownedSource
        val updatedLessons = lessons.mapIndexed { index, element ->
            JsonObject(
                element.jsonObject + (
                    "illustration_source_ref_ids" to JsonArray(
                        if (index == 0) listOf(JsonPrimitive(linkedSource)) else emptyList(),
                    )
                ),
            )
        }
        members["lessons.json"] = JsonArray(updatedLessons).toString().encodeToByteArray()

        val sourceRefs = Json.parseToJsonElement(requireNotNull(members["source_refs.json"]).decodeToString()).jsonArray
        val source = sourceRefs.first { it.jsonObject["source_ref_id"]?.jsonPrimitive?.contentOrNull == ownedSource }.jsonObject
        val page = source.getValue("page_start").jsonPrimitive.content.toInt()
        val metadata = JsonObject(
            mapOf(
                "illustration_id" to JsonPrimitive("illustration_codec_test"),
                "source_ref_id" to JsonPrimitive(ownedSource),
                "asset_member" to JsonPrimitive(assetName),
                "mime_type" to JsonPrimitive(mimeType),
                "sha256" to JsonPrimitive(sha256(image)),
                "byte_size" to JsonPrimitive(image.size),
                "width" to JsonPrimitive(1),
                "height" to JsonPrimitive(1),
                "page_number" to JsonPrimitive(page),
                "caption" to JsonPrimitive("Một điểm ảnh minh họa"),
                "alt_text" to JsonPrimitive("Ảnh vuông một điểm ảnh"),
            ) + overrides,
        )
        members["illustrations.json"] = JsonArray(listOf(metadata)).toString().encodeToByteArray()
        members[assetName] = image

        val originalManifest = bundledManifest()
        val memberHashes = JsonObject(members.mapValues { JsonPrimitive(sha256(it.value)) })
        members["manifest.json"] = JsonObject(originalManifest + ("members" to memberHashes)).toString().encodeToByteArray()
        return writeZip(members)
    }

    private fun readBundledMembers(): MutableMap<String, ByteArray> {
        val result = linkedMapOf<String, ByteArray>()
        ZipInputStream(File("src/main/assets/content/default.dlpack").inputStream()).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                result[entry.name] = zip.readBytes()
                zip.closeEntry()
            }
        }
        return result
    }

    private fun bundledManifest(): JsonObject {
        val members = readBundledMembers()
        return Json.parseToJsonElement(requireNotNull(members["manifest.json"]).decodeToString()).jsonObject
    }

    private fun writeZip(members: Map<String, ByteArray>): ByteArray {
        val output = ByteArrayOutputStream()
        ZipOutputStream(output).use { zip ->
            members.forEach { (name, payload) ->
                zip.putNextEntry(ZipEntry(name))
                zip.write(payload)
                zip.closeEntry()
            }
        }
        return output.toByteArray()
    }

    private fun sha256(payload: ByteArray): String = "sha256:" + MessageDigest.getInstance("SHA-256")
        .digest(payload).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun onePixelPng(): ByteArray = Base64.getDecoder().decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    )

    private fun onePixelWebpHeader(): ByteArray = byteArrayOf(
        0x52, 0x49, 0x46, 0x46, 22, 0, 0, 0,
        0x57, 0x45, 0x42, 0x50,
        0x56, 0x50, 0x38, 0x58, 10, 0, 0, 0,
        0, 0, 0, 0,
        0, 0, 0,
        0, 0, 0,
    )
}
