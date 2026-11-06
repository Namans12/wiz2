package com.wizlight.app

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LatestWizSenderTest {
    @Test fun batchSendsEveryFirstPacketBeforeAnyRetries() {
        val calls = Collections.synchronizedList(mutableListOf<String>())
        val firstRetries = CountDownLatch(3)
        val sender = LatestWizSender({ ip, _ ->
            calls.add(ip)
            firstRetries.countDown()
        }, {}, retryGapMs = 10)
        try {
            assertTrue(sender.offerBatch(linkedMapOf("a" to byteArrayOf(1), "b" to byteArrayOf(2))))
            assertTrue(firstRetries.await(2, TimeUnit.SECONDS))
        } finally {
            sender.close()
        }
        assertEquals(listOf("a", "b"), calls.take(2))
    }
    @Test fun pendingUpdatesAreCoalescedPerLightAndOldRetriesStop() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val updated = CountDownLatch(2)
        val calls = Collections.synchronizedList(mutableListOf<String>())
        val sender = LatestWizSender({ ip, bytes ->
            val value = String(bytes)
            calls.add("$ip:$value")
            if (ip == "a" && value == "old") {
                entered.countDown()
                assertTrue(release.await(2, TimeUnit.SECONDS))
            }
            if (value == "new" || ip == "b") updated.countDown()
        }, {}, retryGapMs = 5)
        try {
            assertTrue(sender.offer("a", "old".toByteArray()))
            assertTrue(entered.await(2, TimeUnit.SECONDS))
            sender.offer("a", "stale".toByteArray())
            sender.offer("a", "new".toByteArray())
            sender.offer("b", "other".toByteArray())
            release.countDown()
            assertTrue(updated.await(2, TimeUnit.SECONDS))
        } finally {
            release.countDown()
            sender.close()
        }
        assertEquals(1, calls.count { it == "a:old" })
        assertFalse(calls.contains("a:stale"))
        assertTrue(calls.contains("a:new"))
        assertTrue(calls.contains("b:other"))
        assertFalse(sender.offer("a", "late".toByteArray()))
    }

    @Test fun reportsSendFailures() {
        val failure = CountDownLatch(1)
        val sender = LatestWizSender({ _, _ -> throw IllegalStateException("offline") },
            { if (it != null) failure.countDown() })
        try {
            sender.offer("a", byteArrayOf(1))
            assertTrue(failure.await(2, TimeUnit.SECONDS))
        } finally {
            sender.close()
        }
    }

    @Test fun callbackFailureDoesNotStopWorker() {
        val completed = CountDownLatch(2)
        val sender = LatestWizSender({ _, _ -> completed.countDown() },
            { throw IllegalStateException("diagnostic callback failed") })
        try {
            sender.offerBatch(linkedMapOf("a" to byteArrayOf(1), "b" to byteArrayOf(2)))
            assertTrue(completed.await(2, TimeUnit.SECONDS))
        } finally {
            sender.close()
        }
    }
}
