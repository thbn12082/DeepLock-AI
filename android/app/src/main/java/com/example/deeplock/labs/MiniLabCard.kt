package com.example.deeplock.labs

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.example.deeplock.data.content.MiniLab
import java.util.Locale

@Composable
fun MiniLabCard(lab: MiniLab, modifier: Modifier = Modifier) {
    val decoded = remember(lab.lab_type, lab.spec) { MiniLabDecoder.decode(lab) }
    var resetVersion by remember(lab.mini_lab_id) { mutableIntStateOf(0) }

    Card(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Mini Lab · ${lab.lab_type}", style = MaterialTheme.typography.titleMedium)
            when (decoded) {
                is MiniLabDecodeResult.Ready -> key(resetVersion) { RenderLab(decoded.spec) }
                is MiniLabDecodeResult.Fallback -> Text(
                    "Không thể mở phần tương tác (${decoded.reason}). Bản mô tả bên dưới vẫn dùng được.",
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            TextButton(onClick = { resetVersion++ }) { Text("Đặt lại") }
            TextualFallback(lab)
        }
    }
}

@Composable
private fun RenderLab(spec: MiniLabSpec) {
    when (spec) {
        is MiniLabSpec.LearningRateStep1D -> LearningRateLab(spec)
        is MiniLabSpec.ActivationCurve -> ActivationLab(spec)
        is MiniLabSpec.Convolution2D -> ConvolutionLab(spec)
        is MiniLabSpec.OverfittingCurve -> OverfittingLab(spec)
        is MiniLabSpec.ConfusionMatrix -> ConfusionLab(spec)
    }
}

@Composable
private fun LearningRateLab(spec: MiniLabSpec.LearningRateStep1D) {
    var rate by remember(spec) { mutableFloatStateOf(spec.learningRateDefault.toFloat()) }
    val safeRate = rate.toDouble().coerceIn(spec.learningRateMin, spec.learningRateMax)
    Text("Kéo learning rate để quan sát đúng một bước cập nhật.")
    if (spec.learningRateMin < spec.learningRateMax) {
        Slider(
            value = rate,
            onValueChange = { rate = it },
            valueRange = spec.learningRateMin.toFloat()..spec.learningRateMax.toFloat(),
        )
    } else {
        Text("Learning rate cố định: ${format(spec.learningRateDefault)}")
    }
    Text("w mới = ${format(LabMath.gradientStep(spec.initialWeight, spec.gradient, safeRate))}  (η = ${format(safeRate)})")
}

@Composable
private fun ActivationLab(spec: MiniLabSpec.ActivationCurve) {
    var x by remember(spec) { mutableFloatStateOf(0f.coerceIn(spec.xMin.toFloat(), spec.xMax.toFloat())) }
    val safeX = x.toDouble().coerceIn(spec.xMin, spec.xMax)
    Text("${spec.function}(${format(safeX)}) = ${format(LabMath.activation(spec.function, safeX))}")
    Slider(value = x, onValueChange = { x = it }, valueRange = spec.xMin.toFloat()..spec.xMax.toFloat())
    CurveCanvas(spec.xMin, spec.xMax, spec.samples, spec.function) { LabMath.activation(spec.function, it) }
}

@Composable
private fun ConvolutionLab(spec: MiniLabSpec.Convolution2D) {
    val output = remember(spec) { LabMath.convolution(spec.input, spec.kernel, spec.stride) }
    Text("Input ${spec.input.size}×${spec.input.first().size} · kernel ${spec.kernel.size}×${spec.kernel.first().size} · stride ${spec.stride}")
    Matrix(output)
}

@Composable
private fun OverfittingLab(spec: MiniLabSpec.OverfittingCurve) {
    val first = remember(spec) { LabMath.firstOverfitEpoch(spec.trainLoss, spec.validationLoss) }
    Text(first?.let { "Dấu hiệu overfitting đầu tiên ở epoch $it." } ?: "Chưa thấy điểm phân kỳ đơn điệu rõ ràng.")
    DualCurveCanvas(spec.trainLoss, spec.validationLoss)
}

@Composable
private fun ConfusionLab(spec: MiniLabSpec.ConfusionMatrix) {
    val metrics = remember(spec) { LabMath.confusionMetrics(spec.matrix) }
    Text("Accuracy ${format(metrics.accuracy * 100)}% · Macro recall ${format(metrics.macroRecall * 100)}%")
    Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        Column { Text("Thật↓ / Đoán→"); spec.labels.forEach { Text(it, Modifier.padding(6.dp)) } }
        spec.labels.indices.forEach { column ->
            Column {
                Text(spec.labels[column])
                spec.matrix.indices.forEach { row -> Text(spec.matrix[row][column].toString(), Modifier.padding(6.dp)) }
            }
        }
    }
}

/** Always-present TalkBack/font-scaling-safe representation; also serves invalid renderers. */
@Composable
private fun TextualFallback(lab: MiniLab) {
    Column(
        Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(10.dp)
            .semantics(mergeDescendants = true) {},
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Text("Mô tả dạng văn bản", style = MaterialTheme.typography.labelLarge)
        if (lab.fallback.columns.isNotEmpty()) Text(lab.fallback.columns.joinToString(" | "))
        if (lab.fallback.rows.isEmpty()) {
            Text("Không có bảng dữ liệu đóng gói.")
        } else {
            lab.fallback.rows.forEachIndexed { index, row -> Text("${index + 1}. ${row.joinToString(" | ")}") }
        }
        lab.fallback.takeaways.forEach { Text("• $it") }
    }
}

@Composable
private fun Matrix(matrix: List<List<Double>>) {
    Column(Modifier.background(MaterialTheme.colorScheme.surfaceVariant).padding(8.dp)) {
        matrix.forEach { row -> Text(row.joinToString("    ") { format(it) }) }
    }
}

@Composable
private fun CurveCanvas(min: Double, max: Double, samples: Int, name: String, function: (Double) -> Double) {
    val curveColor = MaterialTheme.colorScheme.primary
    Canvas(
        Modifier
            .fillMaxWidth()
            .height(130.dp)
            .semantics { contentDescription = "Đồ thị $name từ ${format(min)} đến ${format(max)}" },
    ) {
        val values = (0..samples).map { i -> min + (max - min) * i / samples }.map(function)
        val low = minOf(values.minOrNull() ?: 0.0, 0.0)
        val high = maxOf(values.maxOrNull() ?: 1.0, 1.0)
        for (i in 1..samples) {
            drawLine(
                curveColor,
                Offset(size.width * (i - 1) / samples, size.height * (1 - ((values[i - 1] - low) / (high - low))).toFloat()),
                Offset(size.width * i / samples, size.height * (1 - ((values[i] - low) / (high - low))).toFloat()),
                strokeWidth = 4f,
            )
        }
    }
}

@Composable
private fun DualCurveCanvas(first: List<Double>, second: List<Double>) {
    val trainColor = MaterialTheme.colorScheme.primary
    val validationColor = MaterialTheme.colorScheme.error
    Canvas(
        Modifier
            .fillMaxWidth()
            .height(130.dp)
            .semantics { contentDescription = "Đồ thị train loss và validation loss theo epoch" },
    ) {
        val all = first + second
        val low = all.minOrNull() ?: 0.0
        val high = all.maxOrNull() ?: 1.0
        fun draw(values: List<Double>, color: Color) {
            values.zipWithNext().forEachIndexed { index, pair ->
                val denominator = maxOf(1, values.lastIndex)
                fun y(value: Double) = size.height * (1 - ((value - low) / maxOf(.000001, high - low))).toFloat()
                drawLine(color, Offset(size.width * index / denominator, y(pair.first)), Offset(size.width * (index + 1) / denominator, y(pair.second)), 4f)
            }
        }
        draw(first, trainColor)
        draw(second, validationColor)
    }
}

private fun format(value: Double) = String.format(Locale.US, "%.3f", value)
