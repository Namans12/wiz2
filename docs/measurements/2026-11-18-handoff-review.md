# November 18 handoff review

| Question from the earlier handoff | Current evidence |
|---|---|
| Can one bulb stay off during sync? | Each saved bulb has a participation control. Desktop and Android persist it. Powering off from its card excludes that bulb before issuing the off command. The Windows hardware check used an isolated config with lamp 1 selected. It confirmed all four cuts while lamp 2 stayed off in 16 independent reads. Both lamps were restored. |
| Does the window stutter? | Six Windows DXCam start/stop comparisons with no bulbs showed median Start callback time falling from 304 to 6 ms and Stop from 77 to 20 ms. The median longest event-loop gap fell from 321 to 63 ms. Sidebar navigation median improved, while its p95 showed no improvement. |
| Do occasional long delays persist? | The November 15 optical ABBA run measured wall medians of 450 ms with the monitored link off and 300 ms with it on. The November 18 film run showed overlapping reply outages on both lamps while capture continued advancing. New link statistics retain the longest active reply gap after recovery. Visible delay during those specific outages needs a synchronized recording. |
| Are higher frame rates worthwhile? | The November 15 comparison covered adaptive capture and fixed 15, 30 and 60 FPS. Fixed 60 reached 57–58 FPS with higher CPU. Adaptive remains the default. |
| Are split-screen zones independent? | Both lamps previously reported opposite 45% and 100% dimming values when the regions swapped. Each region now also determines its own neutral-color adaptation. |
| Is capture still the bottleneck? | Sequential two-zone processing and packed BGRA sampling reduced measured CPU work with identical output on film frames. A native GPU downscaler remains an untested option. |
| Does color calibration follow the right lamp? | Discovery preserves exclusions and zones through a simultaneous address swap. Camera settings and stored corrections check the bulb identity. Lamp 2 has its own measured table. Lamp 1 requires independent measurements. |
| Have varied colors and real video been exercised? | Fifteen minutes of film on HDMI, two real lamps, variable home Wi-Fi signal, and controlled UDP impairment are documented in the companion report. Both color models completed 14 tablet-recorded patches each and both lamps were restored. Comparing optical patch colors requires the original recording. |
| Do the controls and packaged apps work? | The Windows GUI opened and closed with a fresh isolated configuration. Tests cover lifecycle races and power-off ordering. The Android APK includes phone and TV launchers and uses the existing debug signing key. |
| What still needs hardware or product decisions? | Physical Android, Bravia and macOS acceptance, visual confirmation of the Windows capture border, absolute color accuracy, hours-long optical testing, and release signing remain open. The desktop single/auto mode still limits smoothing, boost and color delta. Both bulbs previously reported 500 ms power fades. |

The 15-minute film run and impairment results are in [the two-lamp report](2026-11-18-real-video-and-link.md). Earlier optical and capture comparisons are in [the November 15 report](2026-11-15-optical-and-capture.md).

A final 75.8-second two-lamp film replay on the optimized code processed 1,328 frames and submitted 262 updates. Both initial lamp states were verified after restoration. The longest intervals between status replies were 3780 ms and 2420 ms.
