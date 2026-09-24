package com.example.deeplock.lockmode

/**
 * Converts the small LaTeX subset used by bundled lessons into readable
 * Unicode text. This intentionally has no WebView or network dependency: lock
 * cards must render instantly and fully offline.
 */
internal fun humanizeMathText(raw: String): String = MathTextFormatter.format(raw)

private object MathTextFormatter {
    private val mathSignal = Regex(
        """\\(?:frac|sqrt|sum|prod|int|partial|nabla|hat|bar|vec|theta|alpha|beta|gamma|delta|epsilon|eta|lambda|mu|sigma|phi|psi|omega|mathrm|mathbf|mathbb|mathcal|operatorname|log|exp|left|right|times|cdot|rightarrow|to|leq|geq|neq|approx)\b|[A-Za-z][_^](?:\{|[A-Za-z0-9])|->""",
    )
    private val compactVariable = Regex("""\b[A-Za-z][0-9ivjktn]+\b""")
    private val mathOperators = setOf('=', '+', '-', '×', '·', '*', '/', '≈', '<', '>', '≤', '≥', '≠')

    fun format(raw: String): String {
        if (raw.isBlank()) return raw
        val trimmed = raw.trim()
        val compactStandalone = compactVariable.matches(trimmed)
        val compactInExpression = compactVariable.containsMatchIn(raw) && raw.any { it in mathOperators }
        if (!mathSignal.containsMatchIn(raw) && !compactStandalone && !compactInExpression) return raw
        var source = raw.trim()
        if (source.length >= 2 && source.first() == '$' && source.last() == '$') {
            source = source.substring(1, source.lastIndex)
        }
        source = source
            .removeSurrounding("\\(", "\\)")
            .removeSurrounding("\\[", "\\]")

        return cleanSpacing(normalizeCompactVariables(LatexUnicodeParser(source).parse()))
    }

    /**
     * AI-authored examples often use h1/e1v instead of h_1/e_{1v}. Convert
     * only compact variable tokens that are standalone or touch a math
     * operator, leaving normal prose untouched.
     */
    private fun normalizeCompactVariables(value: String): String {
        return Regex("""\b[A-Za-z][A-Za-z0-9]*\b""").replace(value) { match ->
            val token = match.value
            if (token.length < 2) return@replace token
            val suffix = token.substring(1)
            if (!suffix.all { it.isDigit() || it in "ivjktn" }) return@replace token

            val previous = value.substring(0, match.range.first).lastOrNull { !it.isWhitespace() }
            val next = value.substring(match.range.last + 1).firstOrNull { !it.isWhitespace() }
            val standalone = value.trim() == token
            val inExpression = previous in mathOperators || next in mathOperators
            if (!standalone && !inExpression) token
            else token.first() + suffix.asSubscript()
        }
    }

    private fun cleanSpacing(value: String): String = value
        .replace("->", "→")
        .replace(Regex("""\s*([=+×·÷→←≤≥≠≈∝∈∉∪∩])\s*"""), " $1 ")
        .replace(Regex("""\s*,\s*"""), ", ")
        .replace(Regex("""\(\s+"""), "(")
        .replace(Regex("""\s+\)"""), ")")
        .replace(Regex("""\s+"""), " ")
        .trim()
}

private class LatexUnicodeParser(private val source: String) {
    private var index = 0

    fun parse(stopAtClosingBrace: Boolean = false): String = buildString {
        while (index < source.length) {
            when (val char = source[index]) {
                '}' -> {
                    if (stopAtClosingBrace) {
                        index++
                        return@buildString
                    }
                    index++
                }
                '{' -> {
                    index++
                    append(parse(stopAtClosingBrace = true))
                }
                '\\' -> append(parseCommand())
                '_' -> {
                    index++
                    append(readArgument().asSubscript())
                }
                '^' -> {
                    index++
                    append(readArgument().asSuperscript())
                }
                '$' -> index++
                '&' -> {
                    index++
                    append("  ")
                }
                else -> {
                    append(char)
                    index++
                }
            }
        }
    }

    private fun parseCommand(): String {
        index++
        if (index >= source.length) return ""
        if (!source[index].isLetter()) {
            return when (val escaped = source[index++]) {
                '\\' -> " ; "
                ',', ';', ':', '!' -> " "
                '{', '}', '_', '%', '#', '&' -> escaped.toString()
                ' ' -> " "
                else -> escaped.toString()
            }
        }

        val start = index
        while (index < source.length && source[index].isLetter()) index++
        val command = source.substring(start, index)
        return when (command) {
            "frac", "dfrac", "tfrac" -> {
                val numerator = readArgument().trim()
                val denominator = readArgument().trim()
                "($numerator) ÷ ($denominator)"
            }
            "sqrt" -> {
                val degree = readOptionalBracket()
                val radicand = readArgument().trim()
                if (degree.isNullOrBlank() || degree == "2") "√($radicand)"
                else "${degree.asSuperscript()}√($radicand)"
            }
            "binom" -> {
                val total = readArgument().trim()
                val chosen = readArgument().trim()
                "C($total, $chosen)"
            }
            "text", "texttt", "mathrm", "mathbf", "mathbb", "mathcal", "operatorname" -> readArgument()
            "hat" -> readArgument().withCombiningMark('\u0302')
            "bar", "overline" -> readArgument().withCombiningMark('\u0305')
            "tilde" -> readArgument().withCombiningMark('\u0303')
            "vec" -> readArgument().withCombiningMark('\u20D7')
            "xrightarrow" -> {
                val label = readArgument().trim()
                if (label.isBlank()) "→" else "—$label→"
            }
            "begin", "end" -> {
                readRawGroup()
                ""
            }
            "left", "right", "big", "Big", "bigl", "bigr", "Bigl", "Bigr" -> ""
            "quad", "qquad", "enspace" -> " "
            else -> commandSymbols[command] ?: command
        }
    }

    private fun readArgument(): String {
        skipSpaces()
        if (index >= source.length) return ""
        return when (source[index]) {
            '{' -> {
                index++
                parse(stopAtClosingBrace = true)
            }
            '\\' -> parseCommand()
            else -> source[index++].toString()
        }
    }

    private fun readRawGroup(): String {
        skipSpaces()
        if (index >= source.length || source[index] != '{') return ""
        index++
        val start = index
        var depth = 1
        while (index < source.length && depth > 0) {
            when (source[index++]) {
                '{' -> depth++
                '}' -> depth--
            }
        }
        return source.substring(start, (index - 1).coerceAtLeast(start))
    }

    private fun readOptionalBracket(): String? {
        skipSpaces()
        if (index >= source.length || source[index] != '[') return null
        index++
        val start = index
        while (index < source.length && source[index] != ']') index++
        val value = source.substring(start, index)
        if (index < source.length) index++
        return value
    }

    private fun skipSpaces() {
        while (index < source.length && source[index].isWhitespace()) index++
    }

    private companion object {
        val commandSymbols = mapOf(
            "cdot" to "·", "times" to "×", "div" to "÷",
            "to" to "→", "rightarrow" to "→", "Rightarrow" to "⇒", "leftarrow" to "←",
            "leftrightarrow" to "↔", "mapsto" to "↦",
            "sum" to "∑", "prod" to "∏", "int" to "∫", "oint" to "∮",
            "partial" to "∂", "nabla" to "∇", "infty" to "∞",
            "in" to "∈", "notin" to "∉", "subset" to "⊂", "subseteq" to "⊆",
            "supset" to "⊃", "supseteq" to "⊇", "cup" to "∪", "cap" to "∩",
            "le" to "≤", "leq" to "≤", "ge" to "≥", "geq" to "≥",
            "ne" to "≠", "neq" to "≠", "approx" to "≈", "sim" to "∼", "simeq" to "≃",
            "equiv" to "≡", "propto" to "∝", "pm" to "±", "mp" to "∓",
            "ldots" to "…", "cdots" to "⋯", "vdots" to "⋮", "ddots" to "⋱",
            "ell" to "ℓ", "top" to "ᵀ", "perp" to "⊥", "mid" to "∣",
            "lVert" to "‖", "rVert" to "‖", "langle" to "⟨", "rangle" to "⟩",
            "lfloor" to "⌊", "rfloor" to "⌋", "lceil" to "⌈", "rceil" to "⌉",
            "log" to "log", "ln" to "ln", "exp" to "exp", "max" to "max", "min" to "min",
            "arg" to "arg", "tanh" to "tanh", "sin" to "sin", "cos" to "cos",
            "alpha" to "α", "beta" to "β", "gamma" to "γ", "delta" to "δ",
            "epsilon" to "ε", "varepsilon" to "ε", "zeta" to "ζ", "eta" to "η",
            "theta" to "θ", "vartheta" to "ϑ", "iota" to "ι", "kappa" to "κ",
            "lambda" to "λ", "mu" to "μ", "nu" to "ν", "xi" to "ξ", "pi" to "π",
            "rho" to "ρ", "sigma" to "σ", "tau" to "τ", "upsilon" to "υ",
            "phi" to "φ", "varphi" to "ϕ", "chi" to "χ", "psi" to "ψ", "omega" to "ω",
            "Gamma" to "Γ", "Delta" to "Δ", "Theta" to "Θ", "Lambda" to "Λ",
            "Xi" to "Ξ", "Pi" to "Π", "Sigma" to "Σ", "Phi" to "Φ", "Psi" to "Ψ", "Omega" to "Ω",
        )
    }
}

private fun String.withCombiningMark(mark: Char): String = buildString {
    for (char in this@withCombiningMark) {
        append(char)
        if (!char.isWhitespace()) append(mark)
    }
}

private fun String.asSubscript(): String = buildString {
    for (char in this@asSubscript) append(subscriptCharacters[char] ?: char)
}

private fun String.asSuperscript(): String = buildString {
    for (char in this@asSuperscript) append(superscriptCharacters[char] ?: char)
}

private val subscriptCharacters = mapOf(
    '0' to '₀', '1' to '₁', '2' to '₂', '3' to '₃', '4' to '₄',
    '5' to '₅', '6' to '₆', '7' to '₇', '8' to '₈', '9' to '₉',
    '+' to '₊', '-' to '₋', '=' to '₌', '(' to '₍', ')' to '₎',
    'a' to 'ₐ', 'e' to 'ₑ', 'h' to 'ₕ', 'i' to 'ᵢ', 'j' to 'ⱼ',
    'k' to 'ₖ', 'l' to 'ₗ', 'm' to 'ₘ', 'n' to 'ₙ', 'o' to 'ₒ',
    'p' to 'ₚ', 'r' to 'ᵣ', 's' to 'ₛ', 't' to 'ₜ', 'u' to 'ᵤ',
    'v' to 'ᵥ', 'x' to 'ₓ',
)

private val superscriptCharacters = mapOf(
    '0' to '⁰', '1' to '¹', '2' to '²', '3' to '³', '4' to '⁴',
    '5' to '⁵', '6' to '⁶', '7' to '⁷', '8' to '⁸', '9' to '⁹',
    '+' to '⁺', '-' to '⁻', '=' to '⁼', '(' to '⁽', ')' to '⁾',
    'a' to 'ᵃ', 'b' to 'ᵇ', 'c' to 'ᶜ', 'd' to 'ᵈ', 'e' to 'ᵉ',
    'f' to 'ᶠ', 'g' to 'ᵍ', 'h' to 'ʰ', 'i' to 'ⁱ', 'j' to 'ʲ',
    'k' to 'ᵏ', 'l' to 'ˡ', 'm' to 'ᵐ', 'n' to 'ⁿ', 'o' to 'ᵒ',
    'p' to 'ᵖ', 'r' to 'ʳ', 's' to 'ˢ', 't' to 'ᵗ', 'u' to 'ᵘ',
    'v' to 'ᵛ', 'w' to 'ʷ', 'x' to 'ˣ', 'y' to 'ʸ', 'z' to 'ᶻ',
    'A' to 'ᴬ', 'B' to 'ᴮ', 'D' to 'ᴰ', 'E' to 'ᴱ', 'G' to 'ᴳ',
    'H' to 'ᴴ', 'I' to 'ᴵ', 'J' to 'ᴶ', 'K' to 'ᴷ', 'L' to 'ᴸ',
    'M' to 'ᴹ', 'N' to 'ᴺ', 'O' to 'ᴼ', 'P' to 'ᴾ', 'R' to 'ᴿ',
    'T' to 'ᵀ', 'U' to 'ᵁ', 'V' to 'ⱽ', 'W' to 'ᵂ',
)
