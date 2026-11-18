# Windows film playback and link recovery — 2026-11-18

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
