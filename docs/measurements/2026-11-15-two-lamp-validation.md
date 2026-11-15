# Two-lamp state validation and stale-color recovery — 2026-11-15

## Scope

Windows source checkout, both saved lamps found by MAC at their new DHCP
addresses. The saved addresses were refreshed with a settings backup first.
Tests used the user's selected experimental physical mapper. The inputs here
were synthetic split-screen arrays, not captured video. The raw sanitized
results are in `2026-11-15-link-validation.json`.

These are command-to-reported-state measurements. They do not measure visible
screen-to-light delay, perceptual brightness, flicker or calibrated color
accuracy. Observer queries at a nominal 50 ms cadence were identical in both
link modes, but can influence the bulb's radio behavior. Windows scheduling,
Wi-Fi contention and observer round-trip time are included.

## Per-lamp brightness and colors

ABBA order compared the existing burst/retry sender (A) with the monitored
keep-awake sender (B). Each segment used red, green, blue, cyan, magenta,
yellow, white, gray, dark gray, orange, violet and black. One half of the
synthetic frame was a quarter of the other half's RGB value, swapping the
bright side between cases. All **96 of 96 lamp states matched** their expected
channels, power state and dimming within the 2.5-second observation window.

For red, the lamps reported dimming **45% / 100%**; for the next green case,
they reported **100% / 45%**. The nonlinear brightness mapping and configured
floor mean a quarter RGB input does not imply quarter dimming. The true-black
case respected the existing approximately 0.7-second persistence guard; that
intentional wait is recorded separately from post-dispatch timing.

| Mode | Lamp | Matched | Median | p95 | Maximum |
|---|---|---|---|---|---|
| Link off | 1 | 24/24 | 300 ms | 670 ms | 920 ms |
| Link on | 1 | 24/24 | 169 ms | 486 ms | 707 ms |
| Link off | 2 | 24/24 | 269 ms | 835 ms | 923 ms |
| Link on | 2 | 24/24 | 96 ms | 676 ms | 780 ms |

Both on blocks improved median response relative to the first off block;
the final off block was also faster than the first. Network conditions varied,
so these small samples do not isolate a universal speedup. They do not reproduce
the earlier one-lamp 18 ms median or prove that rare 1–2 second optical stalls
have disappeared.

## Recovery and five-minute run

After a blue state was confirmed, an independent command switched each lamp
off. No new frame was submitted. Both monitors detected the persistent
mismatch, resent the current color, and reconfirmed it. Detection-to-confirmation
was **55 ms and 58 ms**, each with one resend. Initial confirmation counts
remained one per lamp; recovery counters incremented separately.

A 300-second run then alternated five color cuts per second with static
periods. It submitted 4,839 frame attempts; black-hold frames produced no
update, leaving 4,584 sender groups per lamp. Both sender queues reported
zero dropped groups and zero local socket-send errors. That is not proof
of zero wireless packet loss or zero missed visible colors.

- Lamp 1: 7,766 queries, 7,279 replies, 487 timeouts.
- Lamp 2: 8,810 queries, 8,351 replies, 459 timeouts.
- End-of-run state was confirmed on both lamps.
- Initial changed-state confirmations were 418/926 and 428/926. Rapid updates
  supersede older expectations; these fractions cannot be interpreted as
  packet delivery or optical frame-loss rates.
- Sampled RSSI ranged from -82 to -72 dBm on lamp 1 and -75 to -66 dBm on
  lamp 2. The final sample was -71/-68 dBm. This was a variable, weak-signal
  session, not a controlled comparison of multiple Wi-Fi networks.
- Process CPU was 18.81 seconds over 300.04 seconds (about 6.3% of one logical
  core), including the synthetic frame generator. No live screen capture was
  included. Some development checks ran concurrently, so this is indicative.

Both original lamp states were restored and checked successfully afterward.

## Software verification and remaining work

317 Python tests passed with the hardware-marked test excluded; repository
Ruff checks passed. Flutter's 19 tests passed, including remote-navigation
checks. The attached Android phone was detected, but no new screen-sharing
session or Android/TV sender parity is established by these desktop tests.

Remaining: optical ABBA analysis, a capture FPS/CPU comparison, calibrated
multi-color measurements, hours-long operation, controlled network impairment,
and physical Google TV/macOS acceptance. The WinRT no-frame poll loop now
paces immediate returns instead of busy-spinning, with regression tests for
both immediate and blocking polls.
