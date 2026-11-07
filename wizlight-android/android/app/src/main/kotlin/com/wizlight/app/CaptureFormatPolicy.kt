package com.wizlight.app

/** A requested buffer format is not proof of the producer's frame dataspace. */
internal object CaptureFormatPolicy {
    fun isFp16Pq(pixelStride: Int, actualDataSpace: Int, bt2020Pq: Int): Boolean =
        pixelStride >= 8 && actualDataSpace == bt2020Pq

    fun <T> acquire(hdrReader: Boolean, read: () -> T?, fallback: () -> Unit): T? = try {
        read()
    } catch (error: RuntimeException) {
        if (!hdrReader) throw error
        fallback()
        null
    }
}
