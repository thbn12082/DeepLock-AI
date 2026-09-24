package com.example.deeplock.lockmode

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/** A parsed top-level fraction and the expression around it. */
internal data class FractionLayout(
    val prefix: String,
    val numerator: String,
    val denominator: String,
    val suffix: String,
)

/**
 * Extracts the first LaTeX fraction without flattening it to a division sign.
 * Numerator and denominator keep their source so nested fractions can be
 * rendered recursively.
 */
internal fun parseFirstFraction(raw: String): FractionLayout? {
    val source = stripMathDelimiters(raw)
    val commands = listOf("\\dfrac", "\\tfrac", "\\frac")
    var commandStart = -1
    var commandLength = 0
    var cursor = 0
    while (cursor < source.length && commandStart < 0) {
        val command = commands.firstOrNull { source.startsWith(it, cursor) }
        if (command != null) {
            commandStart = cursor
            commandLength = command.length
        } else {
            cursor++
        }
    }
    if (commandStart < 0) return null

    cursor = commandStart + commandLength
    while (cursor < source.length && source[cursor].isWhitespace()) cursor++
    val numerator = readBalancedGroup(source, cursor) ?: return null
    cursor = numerator.second
    while (cursor < source.length && source[cursor].isWhitespace()) cursor++
    val denominator = readBalancedGroup(source, cursor) ?: return null

    return FractionLayout(
        prefix = source.substring(0, commandStart),
        numerator = numerator.first,
        denominator = denominator.first,
        suffix = source.substring(denominator.second),
    )
}

@Composable
internal fun FormulaFocusBlock(latex: String) {
    Card(
        Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
    ) {
        Column(
            Modifier.fillMaxWidth().padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                "Công thức",
                color = MaterialTheme.colorScheme.primary,
                fontWeight = FontWeight.Bold,
            )
            Box(
                Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                contentAlignment = Alignment.CenterStart,
            ) {
                FormulaExpression(
                    source = stripMathDelimiters(latex),
                    style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Normal),
                )
            }
        }
    }
}

@Composable
private fun FormulaExpression(
    source: String,
    style: TextStyle,
    modifier: Modifier = Modifier,
) {
    val fraction = remember(source) { parseFirstFraction(source) }
    if (fraction == null) {
        Text(
            text = humanizeMathText(source),
            modifier = modifier,
            style = style,
            softWrap = false,
        )
        return
    }

    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (fraction.prefix.isNotBlank()) {
            FormulaExpression(fraction.prefix, style)
        }
        StackedFraction(
            numerator = fraction.numerator,
            denominator = fraction.denominator,
            style = style,
        )
        if (fraction.suffix.isNotBlank()) {
            FormulaExpression(fraction.suffix, style)
        }
    }
}

@Composable
private fun StackedFraction(
    numerator: String,
    denominator: String,
    style: TextStyle,
) {
    Column(
        // Min intrinsic width may stop at an operator such as '+', clipping
        // the rest of the numerator/denominator. Max keeps the complete
        // expression visible and sizes the fraction bar to the wider side.
        modifier = Modifier.width(IntrinsicSize.Max),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        FormulaExpression(
            source = numerator,
            style = style,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
        )
        HorizontalDivider(
            modifier = Modifier.fillMaxWidth(),
            thickness = 1.5.dp,
            color = MaterialTheme.colorScheme.onSurface,
        )
        FormulaExpression(
            source = denominator,
            style = style,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
        )
    }
}

private fun stripMathDelimiters(raw: String): String {
    var source = raw.trim()
    if (source.length >= 2 && source.first() == '$' && source.last() == '$') {
        source = source.substring(1, source.lastIndex)
    }
    return source
        .removeSurrounding("\\(", "\\)")
        .removeSurrounding("\\[", "\\]")
}

private fun readBalancedGroup(source: String, start: Int): Pair<String, Int>? {
    if (start >= source.length || source[start] != '{') return null
    var cursor = start + 1
    var depth = 1
    while (cursor < source.length) {
        when (source[cursor]) {
            '{' -> depth++
            '}' -> {
                depth--
                if (depth == 0) {
                    return source.substring(start + 1, cursor) to cursor + 1
                }
            }
        }
        cursor++
    }
    return null
}
