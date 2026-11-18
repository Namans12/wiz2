# Windows film playback and link recovery — 2026-11-18

## Fifteen-minute live two-lamp run

Muted FFplay displayed Blender's [Tears of Steel](https://mango.blender.org/download/)
in a borderless 1920×1080 window verified against the HDMI display geometry.
The 12:14 film looped into the opening again. The source engine used adaptive
15–30 FPS, left/right zones, follow brightness, the saved physical mapper,
the monitored link, and a 28 Hz sender cap. It ran on the code preceding the
two processing optimizations below. Runtime and sender snapshots were
sampled roughly once per second. Both starting lamp states were verified
and restored.

- In 900.97 seconds, the engine processed 16,524 frames (18.34 FPS) and
  submitted 4,286 updates. It classified 69 cuts, snapped 97 large color
  jumps and woke early 392 times. The sampled left/right color targets
  differed 838 of 885 times and their requested brightness differed 774 times.
  Global scene luma was below 0.10 in 49 samples.
- Capture used MSS and DXGI during startup and settled on WinRT after
  approximately 12 seconds. The processed-frame counter advanced in every
  sampled interval. No engine error appeared in the snapshots. Medians of
  rolling capture and processing means were respectively 1.34 and 14.37 ms.
  Ordinary grab timing excludes early polls, and isolated rolling tails
  were much higher. The sync process averaged 52% of one logical CPU core
  excluding FFplay. Other development workloads shared the host.
- The sender recorded zero local socket errors on both lamps. It superseded
  227 and 301 pending values in the two latest-wins queues. These counts
  measure local coalescing. The monitors confirmed the last requested state
  for both lamps within the one-second settling interval after capture stopped.
- Lamp 1 reported RSSI from −72 to −62 dBm, with 1,077 query timeouts in
  21,787 attempts. Lamp 2 reported −88 to −65 dBm, with 1,085 timeouts in
  21,884 attempts. The PC stayed on one 5 GHz access point. This captures
  natural signal variation, while the controlled loopback test below
  isolates a specific loss and delay model.
- The monitors had gone at least 1.5 seconds without a reply in 16 and
  20 snapshots respectively, including 10 snapshots for both lamps together.
  Four consecutive flags for lamp 2 at 842.51–845.52 seconds indicate at
  least one approximately 4.5-second gap in status replies. During the
  simultaneous flags the rolling capture means were only 0.39–2.57 ms,
  showing a link-side response gap independent of a capture freeze. An
  unanswered query cannot establish when a color command arrived.

The live data shows that long response gaps remain possible under real Wi-Fi
conditions. It cannot establish optical flicker, missed visible colors or
whether a 1–2 second visual delay occurred. Intermediate color requests
can be superseded before confirmation, so confirmation counts cannot be
used as a delivery or optical frame-loss rate. This run covered 15 minutes.

Source film SHA-256:
`efa9062d9cdb7a338e40ad530dfdf234806743f29ae6a1a136b97ece4e588e8f`.

## Two-zone processing cost

Replay used 240 decoded 720p frames from five 48-frame excerpts at 0:30,
2:30, 5:30, 8:30 and 10:30 of [Tears of Steel](https://mango.blender.org/download/).
Frames used the normal processing downscale and the saved auto color
algorithm, with left and right zones and follow brightness. Two runs for
each mode used the same frames in order. Process CPU and elapsed extraction
time exclude decoding. The host was also running a live screen sync job.

| Two-zone processing | Median per frame | p95 per frame | CPU time per 240 frames |
|---|---:|---:|---:|
| Sequential | 6.69–7.07 ms | 9.35–9.37 ms | 1.52–1.64 s |
| Parallel | 7.55–8.55 ms | 9.79–10.72 ms | 2.30–2.50 s |

The per-frame color outputs matched across all four runs. The standard
desktop capture configuration now defaults to sequential extraction for
two zones. Three or more zones and callers explicitly requesting parallel
extraction keep the parallel path.

## BGRA conversion after GPU readback

The sampler now gathers each contiguous 8-bit BGRA source pixel as one
32-bit word and writes three RGB output channels. Sampling indices, channel
order and output pixels are unchanged. An A/B replay used five decoded
film frames, resized to each display resolution and converted to BGRA before
timing. Each alternating block converted 200 frames. The benchmark ran
alongside the live job and its timing includes contention on this PC.

| Frame size | Previous median | Packed median | Previous CPU per frame | Packed CPU per frame |
|---|---:|---:|---:|---:|
| 1920×1080 | 3.90–5.57 ms | 2.58–2.80 ms | 3.98–5.16 ms | 2.66–2.81 ms |
| 3840×2400 | 15.02 ms | 6.01–6.23 ms | 13.98–15.23 ms | 6.41–6.56 ms |

Outputs were byte-identical on the film frames. The existing sampling
tests also cover odd edges, noncontiguous views and alternating pixel
lines. These figures measure CPU conversion after readback. Direct3D
staging-map time and optical latency were outside this benchmark.

## Controlled sender recovery

A localhost WiZ responder applied `setPilot` and reported its state on
`getPilot`. Each case ran five times per sender mode. Red was established
before the test, then blue and green were submitted 30 ms apart. Impaired
runs dropped every fourth incoming datagram and delayed replies by 70–91 ms.
The outage cases additionally discarded all incoming traffic during the
specified initial window. Timing begins at the green request and ends when
the receiver applies green. The test exercises the UDP sender in isolation.

| Network model | Link off, delivered / median | Link on, delivered / median |
|---|---:|---:|
| Loopback baseline | 5/5 / 46 ms | 5/5 / 12 ms |
| Periodic loss and delayed replies | 5/5 / 46 ms | 5/5 / 15 ms |
| 300 ms outage, then loss and delay | 5/5 / 533 ms | 5/5 / 501 ms |
| 1.5 s outage, then loss and delay | 0/5 within 3 s | 5/5 / 1,595 ms |

After the 1.5-second outage, monitored delivery took 121–144 ms from
restoration to application. All monitored applied states were confirmed.
No outdated payload was applied after green in any run. The blind sender's
settled retry window ends about 450 ms after its last send. The full sync
engine adds its own 1.5-second periodic refresh, which these trials omit.
These results establish behavior under modeled UDP impairment. RF contention,
router queuing and bulb optical response require hardware measurements.
