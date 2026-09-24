package com.example.deeplock.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.unit.dp

internal val NotebookPaper = Color(0xFFFFEFA3)
internal val NotebookInk = Color(0xFF292719)
internal val NotebookBlue = Color(0xFF315DA8)

internal val NotebookColorScheme = lightColorScheme(
    primary = NotebookBlue,
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFDCE6FF),
    onPrimaryContainer = Color(0xFF102A5B),
    inversePrimary = Color(0xFFB5C8FF),
    secondary = Color(0xFF75621F),
    onSecondary = Color(0xFFFFFFFF),
    secondaryContainer = Color(0xFFF6E39A),
    onSecondaryContainer = Color(0xFF2A2200),
    tertiary = Color(0xFF546847),
    onTertiary = Color(0xFFFFFFFF),
    tertiaryContainer = Color(0xFFD7E9C8),
    onTertiaryContainer = Color(0xFF182312),
    background = NotebookPaper,
    onBackground = NotebookInk,
    surface = Color(0xFFFFF8D2),
    onSurface = NotebookInk,
    surfaceVariant = Color(0xFFF3E19A),
    onSurfaceVariant = Color(0xFF5B5539),
    surfaceTint = NotebookBlue,
    inverseSurface = Color(0xFF343226),
    inverseOnSurface = Color(0xFFFFF5CA),
    error = Color(0xFFB3261E),
    onError = Color(0xFFFFFFFF),
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),
    outline = Color(0xFF847B55),
    outlineVariant = Color(0xFFCFC184),
    scrim = Color(0xFF000000),
    surfaceBright = Color(0xFFFFFBE5),
    surfaceDim = Color(0xFFE7D27D),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFFFF6C5),
    surfaceContainer = Color(0xFFFFF0AC),
    surfaceContainerHigh = Color(0xFFF8E69B),
    surfaceContainerHighest = Color(0xFFF0DC88),
)

private val GameShapes = Shapes(
    extraSmall = RoundedCornerShape(10.dp),
    small = RoundedCornerShape(14.dp),
    medium = RoundedCornerShape(20.dp),
    large = RoundedCornerShape(28.dp),
    extraLarge = RoundedCornerShape(32.dp),
)

private val DefaultTypography = Typography()
private val GameTypography = Typography(
    headlineMedium = DefaultTypography.headlineMedium.copy(fontWeight = FontWeight.ExtraBold),
    headlineSmall = DefaultTypography.headlineSmall.copy(fontWeight = FontWeight.Bold),
    titleLarge = DefaultTypography.titleLarge.copy(fontWeight = FontWeight.ExtraBold),
    titleMedium = DefaultTypography.titleMedium.copy(fontWeight = FontWeight.Bold),
    labelLarge = DefaultTypography.labelLarge.copy(fontWeight = FontWeight.ExtraBold),
)

@Composable
fun DeepLockTheme(
    lockTheme: LockScreenThemeSpec? = null,
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = lockTheme?.colorScheme() ?: NotebookColorScheme,
        shapes = GameShapes,
        typography = GameTypography,
        content = content,
    )
}
