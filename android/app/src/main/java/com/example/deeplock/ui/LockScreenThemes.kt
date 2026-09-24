package com.example.deeplock.ui

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.Color

data class LockScreenThemeSpec(
    val id: String,
    val title: String,
    val subtitle: String,
    val accent: Color,
    val background: Color,
    val surface: Color,
    val surfaceAlt: Color,
    val text: Color = Color(0xFF111827),
    val muted: Color = Color(0xFF5B6775),
) {
    fun colorScheme(): ColorScheme = lightColorScheme(
        primary = accent,
        onPrimary = Color.White,
        primaryContainer = surfaceAlt,
        onPrimaryContainer = text,
        secondary = accent,
        onSecondary = Color.White,
        secondaryContainer = surfaceAlt,
        onSecondaryContainer = text,
        tertiary = Color(0xFF43A047),
        onTertiary = Color.White,
        tertiaryContainer = Color(0xFFDFF6E5),
        onTertiaryContainer = Color(0xFF10351C),
        background = background,
        onBackground = text,
        surface = surface,
        onSurface = text,
        surfaceVariant = surfaceAlt,
        onSurfaceVariant = muted,
        surfaceTint = accent,
        error = Color(0xFFB3261E),
        onError = Color.White,
        errorContainer = Color(0xFFFFDAD6),
        onErrorContainer = Color(0xFF410002),
        outline = muted.copy(alpha = 0.45f),
        outlineVariant = muted.copy(alpha = 0.18f),
        scrim = Color.Black,
        surfaceBright = surface,
        surfaceDim = background,
        surfaceContainerLowest = Color.White,
        surfaceContainerLow = surfaceAlt.copy(alpha = 0.58f),
        surfaceContainer = surfaceAlt.copy(alpha = 0.72f),
        surfaceContainerHigh = surfaceAlt.copy(alpha = 0.86f),
        surfaceContainerHighest = surfaceAlt,
    )
}

val availableLockScreenThemes = listOf(
    LockScreenThemeSpec(
        id = "blue",
        title = "Xanh dương",
        subtitle = "Thanh lịch, tin cậy",
        accent = Color(0xFF1E5BB8),
        background = Color(0xFFF4F9FF),
        surface = Color(0xFFFFFFFF),
        surfaceAlt = Color(0xFFEAF3FF),
    ),
    LockScreenThemeSpec(
        id = "green",
        title = "Xanh lá",
        subtitle = "Tươi mát, tích cực",
        accent = Color(0xFF158A48),
        background = Color(0xFFF7FCF7),
        surface = Color(0xFFFFFFFF),
        surfaceAlt = Color(0xFFEFF8EC),
    ),
    LockScreenThemeSpec(
        id = "lavender",
        title = "Tím lavender",
        subtitle = "Sáng tạo, nhẹ nhàng",
        accent = Color(0xFF6B3FA0),
        background = Color(0xFFFCF8FF),
        surface = Color(0xFFFFFFFF),
        surfaceAlt = Color(0xFFF5ECFA),
    ),
    LockScreenThemeSpec(
        id = "peach",
        title = "Cam đào",
        subtitle = "Ấm áp, năng lượng",
        accent = Color(0xFFF06423),
        background = Color(0xFFFFF7F0),
        surface = Color(0xFFFFFFFF),
        surfaceAlt = Color(0xFFFFF0E5),
    ),
    LockScreenThemeSpec(
        id = "blue_gray",
        title = "Xám xanh",
        subtitle = "Hiện đại, tối giản",
        accent = Color(0xFF2F4F67),
        background = Color(0xFFF5F8FA),
        surface = Color(0xFFFFFFFF),
        surfaceAlt = Color(0xFFEDF2F5),
    ),
)

fun lockScreenThemeById(id: String?): LockScreenThemeSpec =
    availableLockScreenThemes.firstOrNull { it.id == id } ?: availableLockScreenThemes.first()
