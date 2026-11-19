# Device display matrix — capture color behavior & targeting

How WizLight's sync pipeline maps onto the displays we target. The core rule
(verified against AOSP/Microsoft/Apple docs, 2026-07): **what matters is the
color space of the CAPTURE BUFFER, not the panel.** On Android and Windows-SDR
the captured pixels are sRGB-encoded regardless of how wide/saturated the panel
is; macOS is the exception (captures carry the display's P3 profile). The
`auto` color-source setting implements exactly this rule.

| Device | Panel | Gamut | HDR | What capture returns | Our handling (`auto`) |
|---|---|---|---|---|---|
| **Sony Bravia 8 II** (K-xxXR8M2, 2025) | QD-OLED, Google TV (Android 12 / API 31) | ~100% DCI-P3, ~90% BT.2020 | Dolby Vision, HDR10, HLG (no HDR10+) | MediaProjection RGBA_8888: **sRGB SDR**, OS-tonemapped (DV included) | `srgb`, no tone map; BRAVIA profile (cool bias +0.10, OLED true-black, sat gate 0.08). DRM apps/HDMI not capturable — USB player mode is the native path |
| **Galaxy Tab S9 Ultra** | 14.6" Dynamic AMOLED 2X 120 Hz | P3-class (Vivid≈P3 / Natural=sRGB modes) | HDR10+ | MediaProjection: **sRGB SDR** (identical in Vivid & Natural) | `srgb`; WIDE_GAMUT_HDR profile (+0.05 cool bias). Panel's Vivid stretch affects the eye, not the capture |
| **Galaxy S21 / S22–S25** | Dynamic AMOLED 2X (S25 LTPO) | 100% DCI-P3 color volume (VDE) | HDR10+ | Same: **sRGB SDR** buffer | `srgb`; WIDE_GAMUT_HDR profile |
| **MacBook Air M4** | 13.6"/15.3" Liquid Retina **IPS LCD (not OLED)**, 60 Hz, 500 nits | 100% Display P3 | None (minor EDR headroom only) | CGDisplay/ScreenCaptureKit: **Display P3** by default | `display-p3` via Quartz detection (the one platform where panel gamut = capture gamut); `mac` profile (+0.05 cool bias) |
| **Vivobook Pro 16X OLED** (K6604/N7600) | 16" Samsung OLED 120/60 Hz | 100% DCI-P3 (Splendid: Native/sRGB/P3 switch) | DisplayHDR True Black 600/500 | HDR **off**: 8-bit **sRGB** desktop (panel may stretch it in Native mode). HDR **on**: DXCam 8-bit gets a clipped, washed SDR conversion — unreliable for colorimetry | `srgb` (Windows auto is now pinned to srgb). Recommend HDR OFF while syncing, or Splendid sRGB mode for 1:1 eye-vs-bulb match |

## Key consequences baked into the code

1. **Android/Windows `auto` = sRGB, no HDR tone map** (`ColorSyncEngine.configureAutoFromDisplay`,
   `screen_sync_v2.resolve_auto_display_profile`). The old behavior applied a
   Rec.2020→sRGB matrix + tone curve to already-sRGB pixels on the Bravia —
   the direct cause of "too bright / too dim / wrong hue" on DV content.
2. **Per-HDR-type tone curves live only in the FP16 capture path** (API 34+),
   where genuinely HDR pixels exist. The Bravia 8 II ships API 31, so its
   captures are always SDR-tonemapped by the TV first.
3. **Device profiles keep their role** (white-point bias, OLED true-black,
   delta caps) — they correct the *panel/tone-map character*, not the buffer
   encoding.
4. **Manual override preserved**: selecting P3/Rec.2020 in any GUI still
   forces that interpretation for exotic setups (e.g., macOS capture forced
   to sRGB upstream, Windows Auto Color Management).
5. **Windows HDR-on is the one unsupported color path** (8-bit duplication is
   clip-converted by the OS). The GUI's capture status still works; accuracy
   there needs the future FP16 DXGI path.

Sources: AOSP SurfaceFlinger dataspace defaults & HDR-screenshot docs,
Microsoft DXGI/Advanced Color docs, Apple ScreenCaptureKit docs, GSMArena
measurements (Tab S9U, S21–S25), Apple/ASUS official specs. Full citation list
in the session research log (2026-07-03).
