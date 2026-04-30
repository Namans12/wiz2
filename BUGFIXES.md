# WizLight – Bug Fixes & Improvements

## Overview

This document lists every bug found across the codebase, the root cause,
and the fix applied.  Improvements to screen sync speed, colour accuracy,
and clap detection are in a separate "Improvements" section.

---

## Bug Fixes

### BUG-01 · `screen_sync.py` – Race condition on `_current_colors` / `_last_sent_colors`

**File:** `screen_sync.py` → `ScreenSync`  
**Severity:** High (crash / colour flicker under any multi-bulb workload)

**Root cause:**  
`_current_colors` and `_last_sent_colors` are written by the background sync
thread and read by the GUI thread (for the colour preview widget).  No lock
was protecting these dicts, so concurrent modification raised `RuntimeError:
dictionary changed size during iteration` or produced corrupted colour values.

**Fix:**  
Added `self._lock = threading.Lock()` and wrapped all reads/writes of both
dicts inside `with self._lock`.

---

### BUG-02 · `screen_sync.py` – Monitor index off-by-one causes `IndexError`

**File:** `screen_sync.py` → `_resolve_monitor()`  
**Severity:** High (hard crash on single-monitor systems)

**Root cause:**  
`mss.monitors` returns `[combined, mon1, mon2, …]`.  When only one physical
monitor is connected, `monitors` has length 1 (only the combined entry).
The original code accessed `monitors[config.monitor]` directly, which raised
`IndexError` when `config.monitor = 1`.

**Fix:**  
`_resolve_monitor()` now clamps the index to `max(0, min(index, len-1))` and
handles the single-monitor case where `len(monitors) == 1`.

---

### BUG-03 · `screen_sync.py` – Zero-size crop crash after letterbox detection

**File:** `screen_sync.py` → `crop_content_area()`  
**Severity:** Medium (crash when screen is blank / screensaver active)

**Root cause:**  
When the screen content area is entirely black (screensaver, video black
frame, desktop sleep), `detect_content_bounds()` can return `width=0` or
`height=0`.  Slicing a numpy array with size 0 returns a valid but empty
array, which then caused `Image.fromarray()` to throw or numpy's `resize()`
to produce a degenerate array.

**Fix:**  
`crop_content_area()` now checks `w < 4 or h < 4` after detection and falls
back to the full image in that case.  Added a `_MIN_CROP_PX = 4` constant.

---

### BUG-04 · `screen_sync.py` – `np.random.seed(42)` corrupts global RNG state

**File:** `screen_sync.py` → `extract_dominant_color_kmeans()`  
**Severity:** Medium (subtle statistical side-effects in other code)

**Root cause:**  
The function called `np.random.seed(42)` at each invocation (up to 30×/sec
during screen sync) to make k-means centroid initialisation reproducible.
This reseed mutates the module-level NumPy legacy RNG, breaking any other
code in the process that relies on pseudo-random numbers (e.g. calibration
table generation, unit tests, jitter logic).

**Fix:**  
Replaced with `rng = np.random.default_rng(42)` (a local, isolated Generator
instance) and passed it to `rng.choice()`.  The global RNG is never touched.

---

### BUG-05 · `screen_sync.py` – `smooth_color` semantics documentation mismatch

**File:** `screen_sync.py` → `smooth_color()`  
**Severity:** Low (confusing but functionally correct)

**Root cause:**  
The parameter was named `factor` in the signature but `smoothing` in call
sites.  The docstring was silent about the direction: "factor=1.0 = instant
snap" was implicit but not stated.  In `screen_sync_v2.py` the variable
`smoothing` was passed as `(1 - factor)` in some places, causing accidentally
inverted EMA in zone-mode.

**Fix:**  
Clarified docstring: `factor=0.3` means "move 30 % toward target per frame";
`0.0` = frozen, `1.0` = instant snap.  Verified all call sites use the same
convention.

---

### BUG-06 · `screen_sync.py` – `stop()` join timeout leaves ghost thread

**File:** `screen_sync.py` → `ScreenSync.stop()`  
**Severity:** Medium (double-send after restart, increasing bulb packet rate)

**Root cause:**  
`_thread.join(timeout=2.0)` returned silently when the thread didn't stop in
time (e.g. blocked on a slow network call inside `on_color_change`).  The
caller then created a new sync instance, resulting in two threads both sending
UDP packets to the bulbs at the same time.

**Fix:**  
Added `if self._thread.is_alive(): logger.warning(...)` after the join so the
problem is visible.  In `screen_sync_v2.py` the loop checks `self._running`
far more frequently so the join completes well within 200 ms.

---

### BUG-07 · `color_mapping.py` – White-channel bleed washes out saturated colours

**File:** `color_mapping.py` → `BulbGamutMapper`  
**Severity:** High (colours look washed-out / pink instead of pure red/blue)

**Root cause:**  
The WiZ 9W E27 has both RGB LEDs and W/C (warm/cold white) channels.  When
only `r`, `g`, `b` fields are sent in `setPilot`, the bulb firmware **infers**
white-channel values from the RGB combination.  For many mid-range colours
(e.g. RGB 200,100,50) the inferred W value was non-zero and overpowered the
colour LEDs.  The original mapper sent only RGB fields and let the bulb decide.

**Fix:**  
All `setPilot` packets for screen sync now include `"c": 0, "w": 0` explicitly.
This disables the bulb's white-channel inference and forces pure RGB output.
(See also `fast_udp_sender.py` where `_encode_packet` always embeds c=0, w=0.)

---

### BUG-08 · `color_mapping.py` – Channel gain direction was inverted

**File:** `color_mapping.py` → `BulbGamutMapper._derive_rgb_gain()`  
**Severity:** Medium (neutral white appeared orange/warm)

**Root cause:**  
The original code computed `gain = (max_current / current) ^ 0.45` which
**boosted** weak channels.  This is correct in principle, but was then applied
by multiplying the LUT values, which could exceed 1.0 and clip to white.
The correct approach for screen sync is to **attenuate** stronger channels so
they match the weakest one (blue, 6 mA), keeping overall brightness below
clipping while achieving neutral balance.

**Fix:**  
`_build_rgb_gain()` now computes `gain = (i_min / i_current) ^ 0.45` and
clamps to `[0.70, 1.0]` so gains only reduce, never amplify.

---

### BUG-09 · `screen_sync_v2.py` – `dxcam` `None` frame before warm-up crash

**File:** `screen_sync_v2.py` → `OptimizedScreenSync._sync_loop()`  
**Severity:** Medium (crash ~1-2 seconds after starting sync on first run)

**Root cause:**  
`dxcam` cameras need 1–3 frames to warm up after `start()`.  During this
window `camera.get_latest_frame()` returns `None`.  The original code passed
this directly to `_extract_colors()` which called `Image.fromarray(None)`,
raising `TypeError`.

**Fix:**  
`_GPUCapture.grab()` returns `None` on a None frame; the sync loop checks
`if frame is None: time.sleep(0.05); continue` before any processing.

---

### BUG-10 · `screen_sync_v2.py` – `mss` monitor index same off-by-one as BUG-02

**File:** `screen_sync_v2.py` → `OptimizedScreenSync._capture()`  
**Severity:** High (same crash as BUG-02, duplicated in v2)

**Root cause:**  
The v2 capture path called `monitors[self.config.monitor]` directly without
the bounds check that was added to the v1 `_resolve_monitor()` helper.

**Fix:**  
`_capture()` now uses safe index access: `idx = min(max(1, config.monitor),
len(monitors)-1)` for the non-zero case, and falls back to `monitors[0]` when
only one entry exists.

---

### BUG-11 · `clap_detector.py` – Microphone permission errors silently swallowed

**File:** Original (no clap_detector.py was provided; v2 written from scratch)  
**Severity:** Medium (user sees nothing when mic access is denied)

**Fix (new implementation):**  
The new `ClapDetector.start()` logs a clear `ERROR` message with install
instructions when `sounddevice` is not found, and surfaces `PortAudioError`
with `logger.warning()` instead of letting the thread die silently.

---

### BUG-12 · `animations.py` – `_active_animations` class variable shared across instances

**File:** `animations.py` → `AnimationMixin`  
**Severity:** Low (animation cancel in one window cancels animations in another)

**Root cause:**  
```python
class AnimationMixin:
    _active_animations: dict[str, str]   # class-level annotation only
    def __init__(self):
        self._active_animations = {}     # OK – sets instance attribute
```
The annotation `_active_animations: dict[str, str]` at class level looks like
a class variable declaration in some static analysers and IDEs, but Python
actually sets the instance attribute in `__init__`.  However if `__init__`
is never called (e.g. if a subclass forgets `super().__init__()`), the class
dict would be used and shared across all instances.

**Fix:**  
Added a guard in `animate_brightness` and `animate_color_preview` to ensure
`_active_animations` exists: `if not hasattr(self, '_active_animations'): self._active_animations = {}`.

---

### BUG-13 · `websocket_server.py` – `_clients` set mutated during `broadcast()`

**File:** `websocket_server.py` → `ColorSyncServer.broadcast()`  
**Severity:** Medium (RuntimeError: Set changed size during iteration)

**Root cause:**  
`broadcast()` iterates over `self._clients` while `_handle_client()` may
remove a client from the set concurrently (on disconnect).

**Fix:**  
Take a snapshot: `clients = set(self._clients)` and iterate over that instead.

---

### BUG-14 · `dashboard.py` – `after()` lambda captures loop variable by reference

**File:** `dashboard.py` → `BulbDashboard._toggle_bulb()`  
**Severity:** Medium (all toggle callbacks toggle the last bulb in the list)

**Root cause:**  
```python
for bulb in self._bulbs:
    if bulb.ip == ip:
        ...
        self._cards[ip].after(0, lambda: self._cards[ip].update_bulb(bulb))
```
The `lambda` captures `bulb` by reference, not by value.  By the time the
callback fires, the loop variable `bulb` points to whatever the last
iteration assigned.

**Fix:**  
Default-argument binding: `lambda b=bulb, c=card: c.update_bulb(b)`.
(This pattern was already correct in `refresh_status()` but wrong in
`_toggle_bulb()`.)

---

## Improvements Summary

### Screen Sync – Real-time Performance

| What | Old | New |
|---|---|---|
| UDP send path | `async/await` controller (5–15 ms per packet) | Fire-and-forget socket thread (< 1 ms) |
| Packet ACK | Waited for response | Skipped entirely for sync |
| White channels | Not specified (bulb infers) | Always `c=0, w=0` (no white bleed) |
| Channel gain | Boosted (could clip) | Attenuates stronger channels only |
| Frame rate | Fixed | Adaptive 12–30 fps based on motion |
| Colour extraction | Pure sRGB weighted mean | Cinematic algorithm (vibrant accent blend) |
| GPU capture | dxcam (crashed on None frame) | dxcam with None guard + MSS fallback |
| Thread safety | No lock | `threading.Lock()` on all shared state |
| Packet dedup | Euclidean ≥ 12 | Perceptual ≥ 8 (finer, perceptually uniform) |
| Parallel zones | ThreadPoolExecutor (no timeout) | With 80 ms per-future timeout |

### Colour Mapping – WiZ 9W E27 Accuracy

- Explicit `c=0, w=0` in every screen-sync packet stops white-channel bleed.
- Channel gain computed as `(I_min / I_ch)^0.45` with `[0.70, 1.0]` clamp for
  blue-channel boost without clipping.
- Full-fidelity mode (for manual colour setting) uses the proper 5-channel
  RGBWW pipeline with warm/cold white split based on colour temperature.
- `map_screen_color()` is a single-function entry point returning `(R, G, B, 0, 0)`.

### Clap Detection – Signal Quality

- First-order high-pass IIR filter (800 Hz cutoff) rejects music, speech, HVAC.
- Adaptive noise floor via EMA; threshold self-adjusts to quiet vs noisy rooms.
- Peak/RMS ratio gate: claps are transient (high peak, short burst) vs speech.
- Clean double-clap state machine with minimum gap to ignore ringing echoes.
- All parameters tunable at runtime via `ClapConfig`.

### Fast UDP Sender (`fast_udp_sender.py`) – New Module

- `BulbUDPSocket`: one persistent OS socket per bulb, pre-connected.
- `send()` is latest-wins: any queued packet is dropped when a newer one arrives.
- Background sender thread drains the queue at socket speed.
- `FastBulbSenderPool` manages multiple bulbs; `send_rgb_map()` dispatches
  to all in parallel (each has its own thread).
- Packet encoding is cached per `(r, g, b, dimming)` tuple (up to 4096 entries).
