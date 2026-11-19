package com.wizlight.app

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BulbProfileProbeTest {
    private fun reply(nowc: String) =
        "{\"method\":\"getModelConfig\",\"env\":\"pro\",\"result\":{\"ps\":1,\"wcr\":20,\"nowc\":$nowc,\"i2cDrv\":[{\"curr\":[9,8,6,24]}]}}"

    @Test fun parsesWhiteCountLikeDesktop() {
        assertEquals(WhiteLayout.NONE, ModelConfigParser.parseWhiteLayout(reply("0")))
        assertEquals(WhiteLayout.SINGLE, ModelConfigParser.parseWhiteLayout(reply("1")))
        assertEquals(WhiteLayout.DUAL, ModelConfigParser.parseWhiteLayout(reply("2")))
        // out of range / non numeric / missing -> desktop default of one white LED
        assertEquals(WhiteLayout.SINGLE, ModelConfigParser.parseWhiteLayout(reply("3")))
        assertEquals(WhiteLayout.SINGLE, ModelConfigParser.parseWhiteLayout(reply("true")))
        assertEquals(WhiteLayout.SINGLE, ModelConfigParser.parseWhiteLayout("{\"result\":{\"wcr\":20}}"))
        // no usable reply -> unknown
        assertNull(ModelConfigParser.parseWhiteLayout(null))
        assertNull(ModelConfigParser.parseWhiteLayout("{\"method\":\"getModelConfig\",\"error\":{\"code\":-32601}}"))
    }

    private fun waitFor(probe: BulbProfileProbe, ip: String): WhiteLayout? {
        val end = System.currentTimeMillis() + 3000
        while (System.currentTimeMillis() < end) {
            probe.layoutOf(ip)?.let { return it }
            Thread.sleep(5)
        }
        return null
    }

    @Test fun probesOncePerIpAndCaches() {
        val calls = AtomicInteger()
        BulbProfileProbe(query = { ip -> calls.incrementAndGet(); reply(if (ip == "a") "1" else "2") }).use { probe ->
            assertEquals(WhiteLayout.SINGLE, waitFor(probe, "a"))
            assertEquals(WhiteLayout.DUAL, waitFor(probe, "b"))
            repeat(5) { probe.layoutOf("a"); probe.layoutOf("b") }
            assertEquals(2, calls.get())
        }
    }

    @Test fun retriesOnceThenBacksOffAndRecovers() {
        val calls = AtomicInteger()
        val now = AtomicLong(1000)
        val answer = java.util.concurrent.atomic.AtomicReference<String?>(null)
        BulbProfileProbe(query = { calls.incrementAndGet(); answer.get() }, clock = { now.get() }, retryAfterMs = 5000).use { probe ->
            assertNull(probe.layoutOf("x"))
            val end = System.currentTimeMillis() + 3000
            while (calls.get() < 2 && System.currentTimeMillis() < end) Thread.sleep(5)
            assertEquals(2, calls.get())                 // first try + one retry
            Thread.sleep(50)
            assertNull(probe.layoutOf("x")); Thread.sleep(50)
            assertEquals(2, calls.get())                 // backed off, no hammering
            now.addAndGet(6000); answer.set(reply("1"))
            assertEquals(WhiteLayout.SINGLE, waitFor(probe, "x"))
        }
    }

    @Test fun neverBlocksTheCaller() {
        val gate = CountDownLatch(1)
        BulbProfileProbe(query = { gate.await(2, TimeUnit.SECONDS); reply("1") }).use { probe ->
            val start = System.nanoTime()
            assertNull(probe.layoutOf("slow"))
            assertTrue((System.nanoTime() - start) / 1_000_000 < 200)
            gate.countDown()
        }
    }
}
