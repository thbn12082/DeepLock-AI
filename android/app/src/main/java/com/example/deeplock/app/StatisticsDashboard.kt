package com.example.deeplock.app

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.example.deeplock.ui.DashboardGreen
import com.example.deeplock.ui.DashboardMuted
import com.example.deeplock.ui.DashboardRed
import com.example.deeplock.ui.DashboardSurface
import com.example.deeplock.ui.DashboardSurfaceHigh
import com.example.deeplock.ui.availableLockScreenThemes
import java.time.DayOfWeek
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.ceil

@Composable
internal fun StatisticsDashboard(
    state: AppUiState,
    modifier: Modifier = Modifier,
    onThemeSelected: (String) -> Unit = {},
    onTestLockCard: () -> Unit = {},
) {
    val zoneId = remember { ZoneId.systemDefault() }
    val now = state.dashboardNow.takeIf { it > 0L } ?: System.currentTimeMillis()
    val knownAtomIds = remember(state.catalog) {
        state.catalog?.atoms?.mapTo(mutableSetOf()) { it.atom_id }.orEmpty()
    }
    val knownQuestionIds = remember(state.catalog) {
        state.catalog?.atoms?.flatMapTo(mutableSetOf()) { it.question_ids }.orEmpty()
    }
    val stats = remember(
        now,
        zoneId,
        knownAtomIds,
        knownQuestionIds,
        state.progress,
        state.mistakes,
        state.attemptActivity,
        state.lessonActivity,
    ) {
        buildDashboardStats(
            nowMillis = now,
            zoneId = zoneId,
            knownAtomIds = knownAtomIds,
            knownQuestionIds = knownQuestionIds,
            progress = state.progress,
            mistakes = state.mistakes,
            attemptActivity = state.attemptActivity,
            lessonActivity = state.lessonActivity,
        )
    }
    val today = remember(now, zoneId) { Instant.ofEpochMilli(now).atZone(zoneId).toLocalDate() }
    val dateLabel = remember(today) {
        today.format(DateTimeFormatter.ofPattern("d 'tháng' M", Locale("vi", "VN")))
    }

    LazyColumn(
        modifier = modifier.fillMaxSize().padding(horizontal = 18.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            Column(Modifier.padding(top = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text("Tổng quan học tập", style = MaterialTheme.typography.headlineMedium)
                Text(
                    dateLabel,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }

        if (state.error != null) {
            item {
                Card(
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer),
                ) {
                    Text(
                        state.error,
                        modifier = Modifier.padding(14.dp),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }

        item { SevenDayActivityCard(stats) }

        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(9.dp),
            ) {
                DashboardMetricCard(
                    value = stats.knownMistakeQuestions.toString(),
                    label = "Câu từng sai",
                    detail = "${stats.dueMistakeQuestions} đến hạn",
                    accent = DashboardRed,
                    modifier = Modifier.weight(1f),
                )
                DashboardMetricCard(
                    value = stats.lessonsCompletedToday.toString(),
                    label = "Bài hôm nay",
                    detail = "${stats.questionsAnsweredToday} câu trả lời",
                    accent = DashboardGreen,
                    modifier = Modifier.weight(1f),
                )
                DashboardMetricCard(
                    value = stats.masteredKnownAtoms.toString(),
                    label = "Đã vững",
                    detail = "/ ${stats.totalKnownAtoms} kiến thức",
                    accent = MaterialTheme.colorScheme.secondary,
                    modifier = Modifier.weight(1f),
                )
            }
        }

        item { TodayFocusCard(stats) }

        item { MasteryProgressCard(stats) }

        item {
            QuickControlsCard(
                state = state,
                onThemeSelected = onThemeSelected,
                onTestLockCard = onTestLockCard,
            )
        }

        item {
            Text(
                "Chỉ tính bài đã hoàn tất và câu trả lời đã được lưu trên thiết bị.",
                modifier = Modifier.fillMaxWidth().padding(bottom = 20.dp),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }

    if (state.loading && state.catalog == null) {
        Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
    }
}

@Composable
private fun TodayFocusCard(stats: DashboardStats) {
    val goal = 3
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DashboardSurface),
    ) {
        Column(Modifier.fillMaxWidth().padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.Bottom) {
                Column {
                    Text("Hôm nay", style = MaterialTheme.typography.titleLarge)
                    Text(
                        "Streak ${stats.sevenDayStreak} ngày",
                        color = DashboardGreen,
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.SemiBold,
                    )
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text("${stats.todayAccuracyPercent}%", color = DashboardGreen, style = MaterialTheme.typography.headlineMedium)
                    Text("độ chính xác", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
                }
            }
            LinearProgressIndicator(
                progress = { (stats.lessonsCompletedToday.toFloat() / goal).coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().height(9.dp),
                color = DashboardGreen,
                trackColor = DashboardSurfaceHigh,
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                SmallStat("Mục tiêu", "${stats.lessonsCompletedToday}/$goal bài")
                SmallStat("Đúng", stats.correctAnswersToday.toString())
                SmallStat("Sai", stats.wrongAnswersToday.toString(), color = DashboardRed)
            }
        }
    }
}

@Composable
private fun MasteryProgressCard(stats: DashboardStats) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DashboardSurface),
    ) {
        Column(Modifier.fillMaxWidth().padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.Bottom) {
                Column {
                    Text("Tiến độ kiến thức", style = MaterialTheme.typography.titleLarge)
                    Text(
                        "${stats.masteredKnownAtoms}/${stats.totalKnownAtoms} đã vững",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Text("${(stats.masteredPercent * 100f).toInt()}%", color = MaterialTheme.colorScheme.secondary, style = MaterialTheme.typography.headlineSmall)
            }
            LinearProgressIndicator(
                progress = { stats.masteredPercent.coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().height(9.dp),
                color = MaterialTheme.colorScheme.secondary,
                trackColor = DashboardSurfaceHigh,
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                SmallStat("Cần ôn", "${stats.dueMistakeQuestions}")
                SmallStat("Từng sai", "${stats.knownMistakeQuestions}", color = DashboardRed)
                SmallStat("Còn lại", (stats.totalKnownAtoms - stats.masteredKnownAtoms).coerceAtLeast(0).toString())
            }
        }
    }
}

@Composable
private fun QuickControlsCard(
    state: AppUiState,
    onThemeSelected: (String) -> Unit,
    onTestLockCard: () -> Unit,
) {
    val heartbeatFresh = state.settings.serviceHeartbeatAt > 0L &&
        System.currentTimeMillis() - state.settings.serviceHeartbeatAt < 120_000L
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DashboardSurface),
    ) {
        Column(Modifier.fillMaxWidth().padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Column {
                    Text("Điều khiển nhanh", style = MaterialTheme.typography.titleLarge)
                    Text(
                        "Lock Review: ${if (state.settings.lockReviewEnabled) "đang bật" else "đang tắt"} · Service: ${if (heartbeatFresh) "OK" else "chưa thấy"}",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Button(onClick = onTestLockCard) { Text("Thử bảng khóa") }
            }
            LazyRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                items(availableLockScreenThemes, key = { it.id }) { theme ->
                    Box(
                        Modifier
                            .size(if (state.settings.lockThemeId == theme.id) 30.dp else 24.dp)
                            .background(theme.accent, CircleShape)
                            .clickable { onThemeSelected(theme.id) }
                            .semantics { contentDescription = "Chọn giao diện ${theme.title}" },
                    )
                }
            }
            TextButton(onClick = onTestLockCard, modifier = Modifier.fillMaxWidth()) {
                Text("Mở thử lesson/quiz như lúc vừa mở khóa")
            }
        }
    }
}

@Composable
private fun SmallStat(label: String, value: String, color: Color = MaterialTheme.colorScheme.onSurface) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(value, color = color, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun DashboardMetricCard(
    value: String,
    label: String,
    detail: String,
    accent: Color,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier.heightIn(min = 138.dp),
        colors = CardDefaults.cardColors(containerColor = DashboardSurface),
    ) {
        Column(
            modifier = Modifier.fillMaxSize().padding(horizontal = 11.dp, vertical = 13.dp),
            verticalArrangement = Arrangement.SpaceBetween,
        ) {
            Box(Modifier.size(8.dp).background(accent, CircleShape))
            Text(
                value,
                color = accent,
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
            )
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(
                    label,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.labelLarge,
                )
                Text(
                    detail,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
private fun SevenDayActivityCard(stats: DashboardStats) {
    val chartSummary = remember(stats.days) {
        stats.days.joinToString(separator = "; ") { day ->
            "${shortDayLabel(day.date.dayOfWeek)}: ${day.questionsAnswered} câu, ${day.wrongAttempts} lần sai"
        }
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DashboardSurface),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(18.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Bottom,
            ) {
                Column {
                    Text("7 ngày qua", style = MaterialTheme.typography.titleLarge)
                    Text(
                        "Số câu đã trả lời",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        stats.totalStudyActions.toString(),
                        color = DashboardGreen,
                        style = MaterialTheme.typography.headlineMedium,
                    )
                    Text(
                        "TB ${formatAverage(stats.averageStudyActions)}/ngày",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }

            ActivityBarChart(
                days = stats.days,
                average = stats.averageStudyActions,
                modifier = Modifier.fillMaxWidth().height(230.dp).semantics {
                    contentDescription = "Biểu đồ hoạt động 7 ngày. $chartSummary"
                },
            )

            Row(Modifier.fillMaxWidth()) {
                stats.days.forEach { day ->
                    Column(
                        modifier = Modifier.weight(1f),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Text(
                            shortDayLabel(day.date.dayOfWeek),
                            color = if (day == stats.days.last()) DashboardGreen else DashboardMuted,
                            style = MaterialTheme.typography.labelMedium,
                        )
                        Text(
                            day.studyActions.toString(),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }

            Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                ChartLegend(DashboardGreen, "Trả lời đúng")
                ChartLegend(DashboardRed, "Trả lời sai")
                ChartLegend(Color(0xFFDDE4E8), "Trung bình", line = true)
            }
        }
    }
}

@Composable
private fun ActivityBarChart(
    days: List<DashboardDayStats>,
    average: Float,
    modifier: Modifier = Modifier,
) {
    Canvas(modifier.background(DashboardSurfaceHigh, MaterialTheme.shapes.large).padding(12.dp)) {
        val chartBottom = size.height - 8.dp.toPx()
        val chartTop = 10.dp.toPx()
        val chartHeight = (chartBottom - chartTop).coerceAtLeast(1f)
        val maximum = maxOf(
            1f,
            days.maxOfOrNull { it.studyActions }?.toFloat() ?: 0f,
            ceil(average).toFloat(),
        )
        val slotWidth = size.width / days.size.coerceAtLeast(1)
        val barWidth = minOf(30.dp.toPx(), slotWidth * 0.48f)
        val radius = CornerRadius(barWidth / 2f, barWidth / 2f)

        days.forEachIndexed { index, day ->
            val centerX = slotWidth * (index + 0.5f)
            val totalHeight = chartHeight * day.studyActions / maximum
            if (day.studyActions == 0) {
                drawCircle(
                    color = DashboardMuted.copy(alpha = 0.45f),
                    radius = 2.5.dp.toPx(),
                    center = Offset(centerX, chartBottom),
                )
                return@forEachIndexed
            }
            val top = chartBottom - totalHeight
            drawRoundRect(
                color = DashboardGreen,
                topLeft = Offset(centerX - barWidth / 2f, top),
                size = Size(barWidth, totalHeight),
                cornerRadius = radius,
            )
            if (day.wrongAttempts > 0) {
                val wrongHeight = (chartHeight * day.wrongAttempts / maximum).coerceAtMost(totalHeight)
                drawRoundRect(
                    color = DashboardRed,
                    topLeft = Offset(centerX - barWidth / 2f, top),
                    size = Size(barWidth, wrongHeight),
                    cornerRadius = radius,
                )
            }
        }

        if (average > 0f) {
            val averageY = chartBottom - chartHeight * average / maximum
            drawLine(
                color = Color(0xFFDDE4E8),
                start = Offset(0f, averageY),
                end = Offset(size.width, averageY),
                strokeWidth = 1.5.dp.toPx(),
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(8.dp.toPx(), 6.dp.toPx())),
            )
        }
    }
}

@Composable
private fun ChartLegend(color: Color, text: String, line: Boolean = false) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
        Box(
            Modifier
                .size(width = if (line) 12.dp else 7.dp, height = if (line) 2.dp else 7.dp)
                .background(color, if (line) MaterialTheme.shapes.extraSmall else CircleShape),
        )
        Text(text, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
    }
}

private fun shortDayLabel(day: DayOfWeek): String = when (day) {
    DayOfWeek.MONDAY -> "T2"
    DayOfWeek.TUESDAY -> "T3"
    DayOfWeek.WEDNESDAY -> "T4"
    DayOfWeek.THURSDAY -> "T5"
    DayOfWeek.FRIDAY -> "T6"
    DayOfWeek.SATURDAY -> "T7"
    DayOfWeek.SUNDAY -> "CN"
}

private fun formatAverage(value: Float): String = when {
    value == 0f -> "0"
    value % 1f == 0f -> value.toInt().toString()
    else -> String.format(Locale.US, "%.1f", value)
}
