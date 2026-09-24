package com.example.deeplock.labs

import com.example.deeplock.data.content.MiniLab
import com.example.deeplock.data.content.MiniLabFallback
import com.google.common.truth.Truth.assertThat
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Test

class MiniLabDecoderTest {
    private val json = Json { ignoreUnknownKeys = false }

    @Test
    fun allFiveWhitelistedFixtureTypesDecodeAndMatchGoldenValues() {
        val labs = listOf(
            labFromFixture("lr_step_1d.json"),
            labFromFixture("activation_curve.json"),
            labFromFixture("convolution_2d.json"),
            labFromFixture("overfitting_curve.json"),
            labFromFixture("confusion_matrix.json"),
        )

        val specs = labs.map { lab ->
            val result = MiniLabDecoder.decode(lab)
            assertThat(result).isInstanceOf(MiniLabDecodeResult.Ready::class.java)
            (result as MiniLabDecodeResult.Ready).spec
        }
        assertThat(specs.map { it::class.simpleName }).containsExactly(
            "LearningRateStep1D",
            "ActivationCurve",
            "Convolution2D",
            "OverfittingCurve",
            "ConfusionMatrix",
        ).inOrder()

        val lr = specs[0] as MiniLabSpec.LearningRateStep1D
        assertThat(LabMath.gradientStep(lr.initialWeight, lr.gradient, lr.learningRateDefault)).isWithin(1e-9).of(1.7)

        val activation = specs[1] as MiniLabSpec.ActivationCurve
        assertThat(LabMath.activation(activation.function, -2.0)).isEqualTo(0.0)
        assertThat(LabMath.activation(activation.function, 2.0)).isEqualTo(2.0)

        val convolution = specs[2] as MiniLabSpec.Convolution2D
        assertThat(LabMath.convolution(convolution.input, convolution.kernel, convolution.stride)).containsExactly(
            listOf(0.0, -1.0),
            listOf(-1.0, 1.0),
        ).inOrder()

        val overfitting = specs[3] as MiniLabSpec.OverfittingCurve
        assertThat(LabMath.firstOverfitEpoch(overfitting.trainLoss, overfitting.validationLoss)).isEqualTo(4)

        val confusion = specs[4] as MiniLabSpec.ConfusionMatrix
        val metrics = LabMath.confusionMetrics(confusion.matrix)
        assertThat(metrics.accuracy).isWithin(1e-9).of(.85)
        assertThat(metrics.macroRecall).isWithin(1e-9).of(.85)
    }

    @Test
    fun malformedKnownTypesNeverThrowAndExposeFallback() {
        val malformed = listOf(
            lab("LR_STEP_1D", buildJsonObject { put("learning_rate_default", -1) }),
            lab("ACTIVATION_CURVE", buildJsonObject { put("function", "NOT_A_FUNCTION") }),
            lab("CONVOLUTION_2D", buildJsonObject { put("stride", 0) }),
            lab("OVERFITTING_CURVE", buildJsonObject { put("train_loss", 1) }),
            lab("CONFUSION_MATRIX", buildJsonObject { put("labels", "not-an-array") }),
        )

        malformed.forEach { value ->
            val result = runCatching { MiniLabDecoder.decode(value) }
            assertThat(result.isSuccess).isTrue()
            val fallback = result.getOrThrow()
            assertThat(fallback).isInstanceOf(MiniLabDecodeResult.Fallback::class.java)
            assertThat((fallback as MiniLabDecodeResult.Fallback).reason).isNotEmpty()
        }
    }

    @Test
    fun unknownTypeNeverThrowsAndExposesFallback() {
        val result = MiniLabDecoder.decode(lab("FUTURE_SCRIPT_LAB", JsonObject(emptyMap())))
        assertThat(result).isInstanceOf(MiniLabDecodeResult.Fallback::class.java)
        assertThat((result as MiniLabDecodeResult.Fallback).reason).contains("không được hỗ trợ")
    }

    @Test
    fun decoderRejectsExtraFieldsAndOutOfBoundsShapes() {
        val extraField = labFromPayload(
            """{"lab_type":"LR_STEP_1D","initial_weight":2,"gradient":3,"learning_rate_min":0.01,"learning_rate_max":1,"learning_rate_default":0.1,"script":"alert(1)"}""",
        )
        assertThat(MiniLabDecoder.decode(extraField)).isInstanceOf(MiniLabDecodeResult.Fallback::class.java)

        val tooLargeInput = (0..9).joinToString(prefix = "[", postfix = "]") { "[1]" }
        val oversized = labFromPayload(
            """{"lab_type":"CONVOLUTION_2D","input":$tooLargeInput,"kernel":[[1]],"stride":1}""",
        )
        assertThat(MiniLabDecoder.decode(oversized)).isInstanceOf(MiniLabDecodeResult.Fallback::class.java)
    }

    @Test
    fun fixedLearningRateFromBundledPackIsSafe() {
        val fixed = labFromPayload(
            """{"lab_type":"LR_STEP_1D","initial_weight":2,"gradient":3,"learning_rate_min":0.1,"learning_rate_max":0.1,"learning_rate_default":0.1}""",
        )
        val result = MiniLabDecoder.decode(fixed)
        assertThat(result).isInstanceOf(MiniLabDecodeResult.Ready::class.java)
        val spec = (result as MiniLabDecodeResult.Ready).spec as MiniLabSpec.LearningRateStep1D
        assertThat(spec.learningRateMin).isEqualTo(spec.learningRateMax)
    }

    private fun labFromFixture(name: String): MiniLab = labFromPayload(fixture(name).readText(Charsets.UTF_8))

    private fun fixture(name: String): File {
        val candidates = listOf(
            File("src/test/java/com/example/deeplock/labs/fixtures/$name"),
            File("../../fixtures/mini_labs/$name"),
            File("../fixtures/mini_labs/$name"),
            File("fixtures/mini_labs/$name"),
        )
        return candidates.firstOrNull(File::isFile) ?: error("Missing fixture: $name")
    }

    private fun labFromPayload(payload: String): MiniLab {
        val root = json.parseToJsonElement(payload).jsonObject
        val type = root.getValue("lab_type").jsonPrimitive.content
        return lab(type, JsonObject(root.filterKeys { it != "lab_type" }))
    }

    private fun lab(type: String, spec: JsonObject) = MiniLab(
        mini_lab_id = "test-${type.lowercase()}",
        atom_id = "atom-test",
        lab_type = type,
        spec = spec,
        fallback = MiniLabFallback(
            columns = listOf("input", "output"),
            rows = listOf(listOf("fixture", "fallback")),
            takeaways = listOf("Bản mô tả vẫn khả dụng."),
        ),
        source_ref_ids = listOf("source-test"),
        ai_review_status = "AI_APPROVED",
    )
}
