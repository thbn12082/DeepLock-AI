package com.example.deeplock.lockmode

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class MathTextFormatterTest {
    @Test
    fun `renders the edge aggregation fraction without raw latex`() {
        val rendered = humanizeMathText("h_v=\\frac{m_1+m_2}{e_{1v}+e_{2v}}")

        assertEquals("hᵥ = (m₁ + m₂) ÷ (e₁ᵥ + e₂ᵥ)", rendered)
        assertFalse(rendered.contains('\\'))
        assertFalse(rendered.contains('{'))
    }

    @Test
    fun `keeps numerator and denominator for stacked fraction rendering`() {
        val fraction = parseFirstFraction("h_v=\\frac{m_1+m_2}{e_{1v}+e_{2v}}")

        assertEquals("h_v=", fraction?.prefix)
        assertEquals("m_1+m_2", fraction?.numerator)
        assertEquals("e_{1v}+e_{2v}", fraction?.denominator)
        assertEquals("", fraction?.suffix)
    }

    @Test
    fun `preserves nested fractions for recursive rendering`() {
        val outer = parseFirstFraction("\\frac{1}{1+\\frac{a}{b}}")
        val nested = parseFirstFraction(requireNotNull(outer).denominator)

        assertEquals("1", outer.numerator)
        assertEquals("1+", requireNotNull(nested).prefix)
        assertEquals("a", nested.numerator)
        assertEquals("b", nested.denominator)
    }

    @Test
    fun `renders nested root sum and greek symbols`() {
        val rendered = humanizeMathText("\\sigma=\\sqrt{\\frac{1}{n}\\sum_{i=1}^{n}x_i^2}")

        assertEquals("σ = √((1) ÷ (n)∑ᵢ₌₁ⁿxᵢ²)", rendered)
        assertFalse(rendered.contains('\\'))
    }

    @Test
    fun `does not rewrite ordinary windows paths`() {
        assertEquals(
            "D:\\raw\\model\\metrics.json",
            humanizeMathText("D:\\raw\\model\\metrics.json"),
        )
    }

    @Test
    fun `moves compact example indices below their variables`() {
        assertEquals(
            "h₁ = 3, h₂ = 5; e₁ᵥ = 2, e₂ᵥ = 4.",
            humanizeMathText("h1=3, h2=5; e1v=2, e2v=4."),
        )
        assertEquals(
            "Tính message: m₁ = 2 × 3 = 6, m₂ = 4 × 5 = 20.",
            humanizeMathText("Tính message: m1=2×3=6, m2=4×5=20."),
        )
        assertEquals("hᵥ = 26/6 ≈ 4.33.", humanizeMathText("hv=26/6≈4.33."))
    }

    @Test
    fun `keeps compact variables in ordinary prose unchanged`() {
        assertEquals("Node v nhận giá trị xấp xỉ 4.33.", humanizeMathText("Node v nhận giá trị xấp xỉ 4.33."))
    }
}
