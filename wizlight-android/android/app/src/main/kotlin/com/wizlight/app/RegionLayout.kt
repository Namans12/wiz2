package com.wizlight.app

/** Normalized sampling areas for independently positioned lights. */
internal object RegionLayout {
    val supported = setOf("left", "right", "top", "bottom", "center")

    fun normalize(region: String): String = if (region in supported) region else "left"

    fun bounds(region: String): DoubleArray = when (normalize(region)) {
        "right" -> doubleArrayOf(0.78, 0.08, 1.0, 0.92)
        "top" -> doubleArrayOf(0.08, 0.0, 0.92, 0.22)
        "bottom" -> doubleArrayOf(0.08, 0.78, 0.92, 1.0)
        "center" -> doubleArrayOf(0.25, 0.25, 0.75, 0.75)
        else -> doubleArrayOf(0.0, 0.08, 0.22, 0.92)
    }
}
