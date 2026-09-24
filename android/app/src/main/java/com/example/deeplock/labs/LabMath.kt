package com.example.deeplock.labs

import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.tanh

object LabMath {
    private const val MAX_ABS_SCALAR = 1_000_000.0
    private const val MAX_COUNT = 1_000_000

    fun gradientStep(weight: Double, gradient: Double, learningRate: Double): Double {
        requireBounded(weight)
        requireBounded(gradient)
        require(learningRate.isFinite() && learningRate in 0.000001..10.0)
        return (weight - learningRate * gradient).also { require(it.isFinite()) }
    }

    fun activation(name: String, x: Double): Double {
        require(x.isFinite() && x in -20.0..20.0)
        return when (name) {
            "RELU" -> maxOf(0.0, x)
            "SIGMOID" -> 1.0 / (1.0 + exp(-x))
            "TANH" -> tanh(x)
            else -> error("Unsupported activation: $name")
        }.also { require(it.isFinite()) }
    }

    fun convolution(input: List<List<Double>>, kernel: List<List<Double>>, stride: Int): List<List<Double>> {
        require(input.size in 1..9 && kernel.size in 1..5 && stride in 1..3)
        val inputWidth = input.first().size
        val kernelWidth = kernel.first().size
        require(inputWidth in 1..9 && kernelWidth in 1..5)
        require(input.all { it.size == inputWidth && it.all(::isBounded) })
        require(kernel.all { it.size == kernelWidth && it.all(::isBounded) })
        require(input.size >= kernel.size && inputWidth >= kernelWidth)
        return (0..input.size - kernel.size step stride).map { row ->
            (0..inputWidth - kernelWidth step stride).map { column ->
                kernel.indices.sumOf { kr ->
                    kernel[kr].indices.sumOf { kc -> input[row + kr][column + kc] * kernel[kr][kc] }
                }.also { require(it.isFinite()) }
            }
        }
    }

    fun firstOverfitEpoch(trainLoss: List<Double>, validationLoss: List<Double>): Int? {
        require(trainLoss.size == validationLoss.size && trainLoss.size in 3..256)
        require((trainLoss + validationLoss).all { it.isFinite() && it in 0.0..MAX_ABS_SCALAR })
        return (1 until trainLoss.size).firstOrNull { index ->
            trainLoss[index] < trainLoss[index - 1] && validationLoss[index] > validationLoss[index - 1]
        }?.plus(1)
    }

    data class ConfusionMetrics(val accuracy: Double, val macroRecall: Double)

    fun confusionMetrics(matrix: List<List<Int>>): ConfusionMetrics {
        require(matrix.size in 2..10 && matrix.all { row -> row.size == matrix.size && row.all { it in 0..MAX_COUNT } })
        val total = matrix.sumOf { row -> row.sumOf { it.toLong() } }
        val diagonal = matrix.indices.sumOf { matrix[it][it].toLong() }
        val recalls = matrix.indices.map { index ->
            val actual = matrix[index].sumOf { it.toLong() }
            if (actual == 0L) 0.0 else matrix[index][index].toDouble() / actual
        }
        return ConfusionMetrics(
            accuracy = if (total == 0L) 0.0 else diagonal.toDouble() / total,
            macroRecall = recalls.average(),
        )
    }

    private fun requireBounded(value: Double) = require(isBounded(value))
    private fun isBounded(value: Double) = value.isFinite() && abs(value) <= MAX_ABS_SCALAR
}
