package com.wizlight.app

import org.junit.Assert.assertEquals
import org.junit.Test

class HdrConversionTest {
    @Test fun alreadyConvertedCaptureBypassesManualSecondConversion() {
        val engine = ColorSyncEngine()
        engine.sourceColorSpace = "rec2020"
        engine.hdrToneMapping = true
        engine.capturedPixelsAreSrgb = true
        val original = ColorSyncEngine.RgbColor(120, 80, 40)
        val method = ColorSyncEngine::class.java.getDeclaredMethod(
            "mapSourceColorToSrgb", ColorSyncEngine.RgbColor::class.java,
        )
        method.isAccessible = true
        assertEquals(original, method.invoke(engine, original))
    }
}
