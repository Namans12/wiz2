# Bulb listen interval and keep-awake link — 2026-11-13

## Scope

One WiZ RGBW bulb (`ESP25_SHRGB_01`, firmware 1.38.0, signal −58 to −63 dBm)
on the home Wi-Fi network, driven from a Windows laptop that is itself on
Wi-Fi (router ping 1–2 ms). All timings are UDP round trips measured on the
PC. Nothing here is an optical measurement; a camera run is still needed to
confirm what the eye sees.

## The bulb listens about once every 205 ms

Read-only `getPilot` queries, each matched to its reply by the echoed `id`:

| Query spacing | Median reply | p95 | No reply |
|---|---|---|---|
| random 120–330 ms (80 queries) | 88 ms | 196 ms | 4 of 80 |
| fixed 250 / 150 / 100 / 75 / 50 / 30 ms | 74–107 ms | 173–263 ms | 1–11 per 60–100 |
| fixed 15 ms | 147 ms | 216 ms | 38 of 100 |

Sending more often did not shorten the wait. Chaining the queries instead
(next query a fixed gap after the previous reply) showed where the time goes:

| Gap after reply | Median reply | Replies under 25 ms |
|---|---|---|
| 0 ms | 6 ms | 59 of 59 |
| 2 ms | 7 ms | 52 of 59 |
| 5 ms | 6 ms | 56 of 59 |
| 8 ms | 197 ms | 2 of 53 |
| 12 ms | 193 ms | 1 of 54 |
| 20 ms | 185 ms | 0 of 54 |
| 35 ms | 170 ms | 0 of 54 |
| 60 ms | 145 ms | 0 of 55 |

For every gap of 8 ms or more, gap plus reply time is about 205 ms, which is
two Wi-Fi beacon periods. The bulb stays responsive for roughly 5 ms after it
transmits, then stops listening until its next scheduled wake. A datagram sent
outside that window waits in the router, and about 5–10% of those got no reply.

A reply-clocked chain held the responsive state for five minutes: 30-second
buckets had a 5.7–5.9 ms median, 0.3–2.5% of replies over 25 ms, and 0–6 lost
out of 2,600–4,450 queries each (about 110 queries per second).

## Colour updates with and without the chain

Forty red/blue `setPilot` updates about one second apart, same bulb:

| | Median | p90 | Maximum | Not delivered |
|---|---|---|---|---|
| Single datagram, bulb left to sleep (time to the bulb's own reply) | 117 ms | 221 ms | 321 ms | 6 of 40 |
| `FastBulbSenderPool(keep_awake=True)` (time until `getPilot` reports the new state) | 18 ms | 32 ms | 219 ms | 0 of 40 |

The two rows time slightly different events, so the comparison is indicative
rather than exact. A 20-second run of the real capture engine with the link on
confirmed updates in a 19.7 ms median with the bulb awake for 98.8% of replies.

## What changed in the app

- `BulbLinkMonitor` (in `src/features/fast_udp_sender.py`) runs the chain
  while sync is on, tells the sender when the bulb is listening (one datagram
  instead of a burst of three), asks for one more copy when the bulb does not
  report the update within 35 ms (up to four times), and cancels the blind
  60/150/450 ms retries once the bulb reports it. It pauses after 120 s with
  no updates.
- The setting is `screen_sync.keep_awake` (default on), shown in the GUI as
  "Low-latency link". With it off the sender behaves as before.

## Limits

- One bulb, one router, one evening. Other WiZ modules and routers may use a
  different listen interval or awake window.
- About 110 small datagrams per second in each direction per bulb while
  syncing. Effects on a crowded network, and on bulb temperature or lifetime
  over long sessions, were not measured.
- The second configured bulb was not tested; its address had changed from
  192.168.1.10 to 192.168.1.9 (found by MAC through discovery).
- Screen capture on moving video still averaged 45 ms (p95 189 ms) in the
  engine run and is now the largest part of the app-side delay.
