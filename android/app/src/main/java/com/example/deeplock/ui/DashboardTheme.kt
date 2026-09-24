package com.example.deeplock.ui

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

internal val DashboardBackground = Color(0xFF080B0D)
internal val DashboardSurface = Color(0xFF15191C)
internal val DashboardSurfaceHigh = Color(0xFF202529)
internal val DashboardGreen = Color(0xFF7FE36A)
internal val DashboardRed = Color(0xFFFF696D)
internal val DashboardMuted = Color(0xFFAAB3B8)

private val DashboardColorScheme = darkColorScheme(
    primary = DashboardGreen,
    onPrimary = Color(0xFF0B2A0C),
    primaryContainer = Color(0xFF173C1B),
    onPrimaryContainer = Color(0xFFC0F6B4),
    secondary = Color(0xFF8AD7F5),
    onSecondary = Color(0xFF062633),
    secondaryContainer = Color(0xFF173844),
    onSecondaryContainer = Color(0xFFC8EEFF),
    tertiary = Color(0xFFFFD166),
    onTertiary = Color(0xFF332500),
    background = DashboardBackground,
    onBackground = Color(0xFFF3F6F7),
    surface = DashboardSurface,
    onSurface = Color(0xFFF3F6F7),
    surfaceVariant = DashboardSurfaceHigh,
    onSurfaceVariant = DashboardMuted,
    error = DashboardRed,
    onError = Color(0xFF3B0005),
    errorContainer = Color(0xFF4C1D22),
    onErrorContainer = Color(0xFFFFDADB),
    outline = Color(0xFF697277),
    outlineVariant = Color(0xFF353B3F),
    surfaceContainerLow = Color(0xFF111517),
    surfaceContainer = DashboardSurface,
    surfaceContainerHigh = DashboardSurfaceHigh,
    surfaceContainerHighest = Color(0xFF292F33),
)

private val DashboardShapes = Shapes(
    extraSmall = RoundedCornerShape(10.dp),
    small = RoundedCornerShape(14.dp),
    medium = RoundedCornerShape(22.dp),
    large = RoundedCornerShape(28.dp),
    extraLarge = RoundedCornerShape(32.dp),
)

private val BaseTypography = Typography()
private val DashboardTypography = Typography(
    headlineLarge = BaseTypography.headlineLarge.copy(fontWeight = FontWeight.Bold),
    headlineMedium = BaseTypography.headlineMedium.copy(fontWeight = FontWeight.Bold),
    titleLarge = BaseTypography.titleLarge.copy(fontWeight = FontWeight.SemiBold),
    titleMedium = BaseTypography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
    labelLarge = BaseTypography.labelLarge.copy(fontWeight = FontWeight.SemiBold),
)

@Composable
fun DashboardTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DashboardColorScheme,
        typography = DashboardTypography,
        shapes = DashboardShapes,
        content = content,
    )
}
