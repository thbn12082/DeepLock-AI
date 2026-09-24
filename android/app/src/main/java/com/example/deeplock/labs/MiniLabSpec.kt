package com.example.deeplock.labs

import com.example.deeplock.data.content.MiniLab
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull

/** Data-only, bounded representation of the five renderer types accepted by Android. */
internal sealed interface MiniLabSpec {
    data class LearningRateStep1D(
        val initialWeight: Double,
        val gradient: Double,
        val learningRateMin: Double,
        val learningRateMax: Double,
        val learningRateDefault: Double,
    ) : MiniLabSpec

    data class ActivationCurve(
        val function: String,
        val xMin: Double,
        val xMax: Double,
        val samples: Int,
    ) : MiniLabSpec

    data class Convolution2D(
        val input: List<List<Double>>,
        val kernel: List<List<Double>>,
        val stride: Int,
    ) : MiniLabSpec

    data class OverfittingCurve(
        val trainLoss: List<Double>,
        val validationLoss: List<Double>,
    ) : MiniLabSpec

    data class ConfusionMatrix(
        val labels: List<String>,
        val matrix: List<List<Int>>,
    ) : MiniLabSpec
}

internal sealed interface MiniLabDecodeResult {
    data class Ready(val spec: MiniLabSpec) : MiniLabDecodeResult
    data class Fallback(val reason: String) : MiniLabDecodeResult
}

/**
 * Converts untrusted pack JSON into the closed renderer model. Every field is checked before a
 * Composable sees it. A bad or future discriminator is isolated to this lab and never escapes as
 * an exception.
 */
internal object MiniLabDecoder {
    private const val MAX_ABS_SCALAR = 1_000_000.0
    private const val MAX_COUNT = 1_000_000
    private val lrKeys = setOf("initial_weight", "gradient", "learning_rate_min", "learning_rate_max", "learning_rate_default")
    private val activationKeys = setOf("function", "x_min", "x_max", "samples")
    private val convolutionKeys = setOf("input", "kernel", "stride")
    private val overfittingKeys = setOf("train_loss", "validation_loss")
    private val confusionKeys = setOf("labels", "matrix")

    fun decode(lab: MiniLab): MiniLabDecodeResult = try {
        val decoded = when (lab.lab_type) {
            "LR_STEP_1D" -> decodeLearningRate(lab.spec)
            "ACTIVATION_CURVE" -> decodeActivation(lab.spec)
            "CONVOLUTION_2D" -> decodeConvolution(lab.spec)
            "OVERFITTING_CURVE" -> decodeOverfitting(lab.spec)
            "CONFUSION_MATRIX" -> decodeConfusion(lab.spec)
            else -> error("Loại Mini Lab không được hỗ trợ")
        }
        MiniLabDecodeResult.Ready(decoded)
    } catch (error: RuntimeException) {
        MiniLabDecodeResult.Fallback(error.message ?: "Thông số Mini Lab không hợp lệ")
    }

    private fun decodeLearningRate(value: JsonObject): MiniLabSpec.LearningRateStep1D {
        value.requireExactKeys(lrKeys)
        val initial = value.number("initial_weight", -MAX_ABS_SCALAR, MAX_ABS_SCALAR)
        val gradient = value.number("gradient", -MAX_ABS_SCALAR, MAX_ABS_SCALAR)
        val min = value.number("learning_rate_min", 0.000001, 10.0)
        val max = value.number("learning_rate_max", 0.000001, 10.0)
        val default = value.number("learning_rate_default", 0.000001, 10.0)
        require(min <= default && default <= max) { "Khoảng learning rate không hợp lệ" }
        LabMath.gradientStep(initial, gradient, default)
        return MiniLabSpec.LearningRateStep1D(initial, gradient, min, max, default)
    }

    private fun decodeActivation(value: JsonObject): MiniLabSpec.ActivationCurve {
        value.requireExactKeys(activationKeys)
        val function = value.text("function")
        require(function in setOf("RELU", "SIGMOID", "TANH")) { "Hàm activation không được hỗ trợ" }
        val min = value.number("x_min", -20.0, 0.0)
        val max = value.number("x_max", 0.0, 20.0)
        require(min < max) { "Khoảng activation phải tăng" }
        val samples = value.integer("samples", 3, 256)
        LabMath.activation(function, min)
        LabMath.activation(function, max)
        return MiniLabSpec.ActivationCurve(function, min, max, samples)
    }

    private fun decodeConvolution(value: JsonObject): MiniLabSpec.Convolution2D {
        value.requireExactKeys(convolutionKeys)
        val input = value.doubleMatrix("input", maxRows = 9, maxColumns = 9)
        val kernel = value.doubleMatrix("kernel", maxRows = 5, maxColumns = 5)
        require(kernel.size <= input.size && kernel.first().size <= input.first().size) { "Kernel lớn hơn input" }
        val stride = value.integer("stride", 1, 3)
        LabMath.convolution(input, kernel, stride)
        return MiniLabSpec.Convolution2D(input, kernel, stride)
    }

    private fun decodeOverfitting(value: JsonObject): MiniLabSpec.OverfittingCurve {
        value.requireExactKeys(overfittingKeys)
        val train = value.doubleArray("train_loss", 3, 256, 0.0, MAX_ABS_SCALAR)
        val validation = value.doubleArray("validation_loss", 3, 256, 0.0, MAX_ABS_SCALAR)
        require(train.size == validation.size) { "Hai chuỗi loss phải cùng độ dài" }
        LabMath.firstOverfitEpoch(train, validation)
        return MiniLabSpec.OverfittingCurve(train, validation)
    }

    private fun decodeConfusion(value: JsonObject): MiniLabSpec.ConfusionMatrix {
        value.requireExactKeys(confusionKeys)
        val labels = value.array("labels").map { element ->
            val primitive = element as? JsonPrimitive
            require(primitive != null && primitive.isString) { "Nhãn confusion matrix phải là chuỗi" }
            primitive.content.also { require(it.isNotBlank() && it.length <= 80) { "Nhãn confusion matrix không hợp lệ" } }
        }
        require(labels.size in 2..10 && labels.distinct().size == labels.size) { "Số lượng/giá trị nhãn không hợp lệ" }
        val rows = value.array("matrix")
        require(rows.size == labels.size) { "Confusion matrix không khớp số nhãn" }
        val matrix = rows.map { rowElement ->
            val row = rowElement as? JsonArray ?: error("Confusion matrix phải là ma trận")
            require(row.size == labels.size) { "Confusion matrix phải vuông" }
            row.map { element -> element.integer(0, MAX_COUNT) }
        }
        LabMath.confusionMetrics(matrix)
        return MiniLabSpec.ConfusionMatrix(labels, matrix)
    }

    private fun JsonObject.requireExactKeys(expected: Set<String>) {
        require(keys == expected) { "Trường Mini Lab không đúng schema" }
    }

    private fun JsonObject.number(name: String, min: Double, max: Double): Double =
        (get(name) ?: error("Thiếu $name")).number(min, max)

    private fun JsonObject.integer(name: String, min: Int, max: Int): Int =
        (get(name) ?: error("Thiếu $name")).integer(min, max)

    private fun JsonObject.text(name: String): String {
        val primitive = get(name) as? JsonPrimitive
        require(primitive != null && primitive.isString) { "$name phải là chuỗi" }
        return primitive.content
    }

    private fun JsonObject.array(name: String): JsonArray = get(name) as? JsonArray ?: error("$name phải là mảng")

    private fun JsonObject.doubleArray(name: String, minSize: Int, maxSize: Int, min: Double, max: Double): List<Double> {
        val array = array(name)
        require(array.size in minSize..maxSize) { "Kích thước $name vượt giới hạn" }
        return array.map { it.number(min, max) }
    }

    private fun JsonObject.doubleMatrix(name: String, maxRows: Int, maxColumns: Int): List<List<Double>> {
        val outer = array(name)
        require(outer.size in 1..maxRows) { "Số hàng $name vượt giới hạn" }
        val rows = outer.map { rowElement ->
            val row = rowElement as? JsonArray ?: error("$name phải là ma trận")
            require(row.size in 1..maxColumns) { "Số cột $name vượt giới hạn" }
            row.map { it.number(-MAX_ABS_SCALAR, MAX_ABS_SCALAR) }
        }
        require(rows.map { it.size }.distinct().size == 1) { "$name phải là ma trận chữ nhật" }
        return rows
    }

    private fun JsonElement.number(min: Double, max: Double): Double {
        val primitive = this as? JsonPrimitive
        require(primitive != null && !primitive.isString) { "Giá trị phải là số" }
        val result = primitive.doubleOrNull
        require(result != null && result.isFinite() && result in min..max) { "Số không hữu hạn hoặc vượt giới hạn" }
        return result
    }

    private fun JsonElement.integer(min: Int, max: Int): Int {
        val primitive = this as? JsonPrimitive
        require(primitive != null && !primitive.isString) { "Giá trị phải là số nguyên" }
        val result = primitive.intOrNull
        require(result != null && result in min..max) { "Số nguyên vượt giới hạn" }
        return result
    }
}
