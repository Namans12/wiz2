package com.wizlight.app

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SyncTargetRemovalTest {
    private fun t(ip: String, r: String = "left") = ColorSyncEngine.BulbTarget(ip, r)

    @Test fun removedIpsOnlyListLightsThatLeft() {
        assertEquals(listOf("b"), removedTargetIps(listOf(t("a"), t("b")), listOf(t("a", "right"))))
        assertEquals(emptyList<String>(), removedTargetIps(listOf(t("a")), listOf(t("a"), t("never"))))
        assertEquals(emptyList<String>(), removedTargetIps(emptyList(), listOf(t("a"))))
    }

    @Test fun offPacketIsPowerOffOnly() {
        assertEquals("{\"method\":\"setPilot\",\"params\":{\"state\":false}}", String(WizOutputPolicy.OFF_PACKET))
    }

    @Test fun offFollowsInvalidationAndNoColourComesAfter() {
        val calls = Collections.synchronizedList(mutableListOf<Pair<String, String>>())
        val colourStarted = CountDownLatch(1)
        val release = CountDownLatch(1)
        val offSent = CountDownLatch(1)
        val sender = LatestWizSender({ ip, payload ->
            val text = String(payload)
            if (text == "colour-old") { colourStarted.countDown(); release.await(2, TimeUnit.SECONDS) }
            calls.add(ip to text)
            if (text == String(WizOutputPolicy.OFF_PACKET)) offSent.countDown()
        }, {}, retryGapMs = 1)
        try {
            sender.offer("gone", "colour-old".toByteArray())
            assertTrue(colourStarted.await(2, TimeUnit.SECONDS))
            sender.offer("gone", "colour-queued".toByteArray())
            Thread { Thread.sleep(50); release.countDown() }.start()
            assertTrue(sender.invalidateThenOffer(mapOf("gone" to WizOutputPolicy.OFF_PACKET)))
            assertTrue(offSent.await(2, TimeUnit.SECONDS))
            Thread.sleep(100)
        } finally {
            sender.close()
        }
        val off = String(WizOutputPolicy.OFF_PACKET)
        val lastOff = calls.indexOfLast { it.second == off }
        assertTrue(calls.indexOfFirst { it.second == off } >= 0)
        assertEquals(calls.size - 1, lastOff)
        assertTrue(calls.none { it.second == "colour-queued" })
    }
}
