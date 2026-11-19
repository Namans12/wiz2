#!/usr/bin/env python3
"""
WizLight - Android Icon and Asset Generator
Builds Android brand assets from the official desktop icon (packaging/assets/wizlight_icon.png):
  - Legacy mipmap launcher PNGs (ic_launcher.png, ic_launcher_round.png)
    for mdpi (48), hdpi (72), xhdpi (96), xxhdpi (144), xxxhdpi (192)
  - Adaptive icon raster foregrounds (ic_launcher_foreground.png)
    for mdpi (108), hdpi (162), xhdpi (216), xxhdpi (324), xxxhdpi (432)
  - Android TV Leanback banners (tv_banner.png, tv_banner_stop.png) at 640x360
  - In-app brand mark (wizlight_mark.png) at 512x512 transparent

Brand Theme:
  - Background: dark night-navy #07081A -> indigo #140F33 -> violet #2A1757
  - Soft logo glow: violet #7B5CF0 -> blue #4FA3FF radial
  - Wordmark gradient: horizontal blue #4FA3FF -> purple #A66CFF
  - Stop badge / accent: coral #EF4444
"""

import math
import os
import sys
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
BG_NAVY = (7, 8, 26)              # #07081A
BG_INDIGO = (20, 15, 51)          # #140F33
BG_VIOLET = (42, 23, 87)          # #2A1757

GLOW_VIOLET = (123, 92, 240)      # #7B5CF0
GLOW_BLUE = (79, 163, 255)        # #4FA3FF

GRAD_BLUE = (79, 163, 255)        # #4FA3FF
GRAD_PURPLE = (166, 108, 255)     # #A66CFF

STOP_CORAL = (239, 68, 68)        # #EF4444
WHITE = (255, 255, 255)

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
BICUBIC = getattr(Image, "Resampling", Image).BICUBIC


def lerp_color(c1, c2, t):
    """Linearly interpolate between two RGB tuples."""
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def create_linear_gradient(width, height, angle_deg=315):
    """
    Generate a smooth 3-stop background gradient:
    #07081A -> #140F33 -> #2A1757
    """
    gw, gh = 256, 256
    img = Image.new("RGB", (gw, gh))
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)

    pixels = []
    for y in range(gh):
        ny = (y / (gh - 1)) - 0.5
        for x in range(gw):
            nx = (x / (gw - 1)) - 0.5
            proj = nx * cos_a + ny * sin_a + 0.5
            proj = max(0.0, min(1.0, proj))
            if proj < 0.5:
                col = lerp_color(BG_NAVY, BG_INDIGO, proj / 0.5)
            else:
                col = lerp_color(BG_INDIGO, BG_VIOLET, (proj - 0.5) / 0.5)
            pixels.append(col)

    img.putdata(pixels)
    return img.resize((width, height), resample=BICUBIC)


def load_base_logo(logo_path):
    """Load the desktop branding icon and crop tight to content."""
    orig = Image.open(logo_path).convert("RGBA")
    bbox = orig.getbbox()
    if bbox:
        orig = orig.crop(bbox)
    return orig


def create_glowing_logo(base_logo, target_height, is_stop=False):
    """
    Resize base_logo preserving aspect ratio and compose a soft radial glow
    behind it (violet #7B5CF0 -> blue #4FA3FF, low opacity).
    """
    bw, bh = base_logo.size
    target_width = max(1, int(target_height * (bw / bh)))
    resized_logo = base_logo.resize((target_width, target_height), resample=LANCZOS)

    glow_pad = int(target_height * 0.16)
    cw = target_width + glow_pad * 2
    ch = target_height + glow_pad * 2

    glow_layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)

    cx = cw // 2
    # Bulb dome is centered in the upper ~45% of the bulb
    cy = glow_pad + int(target_height * 0.42)
    rx = int(target_width * 0.62)
    ry = int(target_height * 0.48)

    # Outer blue glow
    glow_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(GLOW_BLUE[0], GLOW_BLUE[1], GLOW_BLUE[2], 65))
    # Inner violet glow
    irx = int(rx * 0.70)
    iry = int(ry * 0.70)
    glow_draw.ellipse([cx - irx, cy - iry, cx + irx, cy + iry], fill=(GLOW_VIOLET[0], GLOW_VIOLET[1], GLOW_VIOLET[2], 85))

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(max(2, int(target_height * 0.07))))

    result = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    result.alpha_composite(glow_layer)
    result.alpha_composite(resized_logo, (glow_pad, glow_pad))

    if is_stop:
        badge_r = max(4, int(target_height * 0.13))
        # Place stop badge on bottom-right of bulb base
        badge_cx = glow_pad + int(target_width * 0.78)
        badge_cy = glow_pad + int(target_height * 0.84)
        b_draw = ImageDraw.Draw(result)
        # Dark rim
        b_draw.ellipse(
            [badge_cx - badge_r - 2, badge_cy - badge_r - 2,
             badge_cx + badge_r + 2, badge_cy + badge_r + 2],
            fill=(7, 8, 26, 255),
        )
        # Coral badge
        b_draw.ellipse(
            [badge_cx - badge_r, badge_cy - badge_r,
             badge_cx + badge_r, badge_cy + badge_r],
            fill=STOP_CORAL,
        )
        # White stop square
        sq = max(2, int(badge_r * 0.44))
        b_draw.rectangle(
            [badge_cx - sq, badge_cy - sq, badge_cx + sq, badge_cy + sq],
            fill=WHITE,
        )

    return result, glow_pad, target_width, target_height


def generate_legacy_launcher_icon(base_logo, size, is_round=False):
    """
    Generate legacy launcher PNG (ic_launcher.png / ic_launcher_round.png).
    Logo ~78% canvas height, centered on dark gradient with soft glow.
    """
    scale = 4
    hi_size = size * scale

    bg = create_linear_gradient(hi_size, hi_size, angle_deg=315)
    canvas = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))

    pad = int(hi_size * 0.035)
    box = [pad, pad, hi_size - pad, hi_size - pad]
    mask = Image.new("L", (hi_size, hi_size), 0)
    mask_draw = ImageDraw.Draw(mask)

    if is_round:
        mask_draw.ellipse(box, fill=255)
    else:
        corner_rad = int(hi_size * 0.22)
        mask_draw.rounded_rectangle(box, radius=corner_rad, fill=255)

    canvas.paste(bg, (0, 0), mask)

    # Highlight rim
    rim = ImageDraw.Draw(canvas)
    if is_round:
        rim.ellipse(box, outline=(255, 255, 255, 45), width=max(1, scale))
    else:
        rim.rounded_rectangle(box, radius=corner_rad, outline=(255, 255, 255, 45), width=max(1, scale))

    # Centered glowing logo (~78% of canvas height)
    target_h = int(hi_size * 0.78)
    glowing_logo, glow_pad, tw, th = create_glowing_logo(base_logo, target_h, is_stop=False)
    offset_x = (hi_size - tw) // 2 - glow_pad
    offset_y = (hi_size - th) // 2 - glow_pad

    # Composite over the background, clipped to the shape mask. paste() would
    # replace the background with the logo layer's transparent pixels.
    logo_canvas = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
    logo_canvas.alpha_composite(glowing_logo, (offset_x, offset_y))
    logo_canvas.putalpha(ImageChops.multiply(logo_canvas.getchannel("A"), mask))
    canvas.alpha_composite(logo_canvas)

    return canvas.resize((size, size), resample=LANCZOS)


def generate_adaptive_foreground(base_logo, size):
    """
    Generate adaptive icon raster foreground (ic_launcher_foreground.png) at 108dp.
    Logo is scaled to stay strictly within the central 66dp safe zone.
    Canvas is transparent elsewhere.
    """
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Safe zone is 66dp of 108dp. Scale logo to ~58dp so it has generous margin.
    target_h = int(size * (58.0 / 108.0))
    glowing_logo, glow_pad, tw, th = create_glowing_logo(base_logo, target_h, is_stop=False)

    offset_x = (size - tw) // 2 - glow_pad
    offset_y = (size - th) // 2 - glow_pad

    canvas.alpha_composite(glowing_logo, (offset_x, offset_y))
    return canvas


def load_font(font_path, size):
    """Try loading font, falling back gracefully."""
    candidates = [
        font_path,
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def generate_tv_banner(base_logo, width=640, height=360, is_stop=False):
    """
    Generate TV Banner (640x360) at 2x supersampling:
    - Dark gradient background with soft spotlight
    - Logo on left (~240 px tall at 1x)
    - Wordmark on right with horizontal blue #4FA3FF -> purple #A66CFF gradient
    - Vertically centered as one cohesive group
    - No taglines or extra subtitles
    """
    scale = 2
    sw, sh = width * scale, height * scale

    bg = create_linear_gradient(sw, sh, angle_deg=315)
    canvas = bg.convert("RGBA")

    # Ambient spotlight glow behind logo and text
    spot = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    spot_draw = ImageDraw.Draw(spot)
    spot_draw.ellipse([60, 80, 560, 640], fill=(GLOW_BLUE[0], GLOW_BLUE[1], GLOW_BLUE[2], 55))
    spot_draw.ellipse([540, 140, 1220, 580], fill=(GLOW_VIOLET[0], GLOW_VIOLET[1], GLOW_VIOLET[2], 40))
    spot = spot.filter(ImageFilter.GaussianBlur(int(sw * 0.06)))
    canvas.alpha_composite(spot)

    # Logo on the left (~240 px tall at 1x -> 480 px at 2x)
    logo_h = int(240 * scale)
    glowing_logo, glow_pad, lw, lh = create_glowing_logo(base_logo, logo_h, is_stop=is_stop)
    logo_x = int(50 * scale)
    logo_y = (sh - lh) // 2

    canvas.alpha_composite(glowing_logo, (logo_x - glow_pad, logo_y - glow_pad))

    # Right side text setup
    text_x = logo_x + lw + int(40 * scale)
    max_text_x = sw - int(32 * scale)
    available_w = max_text_x - text_x

    text = "Stop WizLight" if is_stop else "WizLight"

    # Start with ~84 px Segoe UI Bold (168 px at 2x) and scale down if needed
    font_size = int(84 * scale)
    font = None
    text_w = 0
    text_h = 0
    tx0 = 0
    ty0 = 0

    while font_size >= int(32 * scale):
        candidate_font = load_font("C:/Windows/Fonts/segoeuib.ttf", font_size)
        if hasattr(candidate_font, "getbbox"):
            bbox = candidate_font.getbbox(text)
            tw = bbox[2] - bbox[0]
            if tw <= available_w:
                font = candidate_font
                text_w = tw
                text_h = bbox[3] - bbox[1]
                tx0 = bbox[0]
                ty0 = bbox[1]
                break
        else:
            font = candidate_font
            text_w = int(len(text) * font_size * 0.55)
            text_h = font_size
            tx0 = 0
            ty0 = 0
            break
        font_size -= 2

    if font is None:
        font = load_font("C:/Windows/Fonts/segoeuib.ttf", int(48 * scale))
        text_w = int(len(text) * 48 * scale * 0.55)
        text_h = int(48 * scale)

    # Vertically center text relative to canvas height
    text_y = (sh - text_h) // 2 - ty0

    # Render text as a mask and composite horizontal blue -> purple gradient
    text_mask = Image.new("L", (sw, sh), 0)
    mask_draw = ImageDraw.Draw(text_mask)
    mask_draw.text((text_x, text_y), text, fill=255, font=font)

    # Create horizontal gradient matching the wordmark
    x_start = text_x + tx0
    x_end = text_x + tx0 + text_w
    denom = max(1.0, float(x_end - x_start))

    grad_1d = Image.new("RGBA", (sw, 1))
    pixels_1d = []
    for x in range(sw):
        t = (x - x_start) / denom
        t = max(0.0, min(1.0, t))
        col = lerp_color(GRAD_BLUE, GRAD_PURPLE, t)
        pixels_1d.append((col[0], col[1], col[2], 255))
    grad_1d.putdata(pixels_1d)
    text_grad = grad_1d.resize((sw, sh), resample=BICUBIC)

    canvas.paste(text_grad, (0, 0), text_mask)

    return canvas.resize((width, height), resample=LANCZOS)


def generate_brand_mark(base_logo, size=512):
    """
    Generate in-app brand mark (wizlight_mark.png) at 512x512 with transparent background.
    """
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    target_h = int(size * 0.82)
    glowing_logo, glow_pad, tw, th = create_glowing_logo(base_logo, target_h, is_stop=False)
    offset_x = (size - tw) // 2 - glow_pad
    offset_y = (size - th) // 2 - glow_pad
    canvas.alpha_composite(glowing_logo, (offset_x, offset_y))
    return canvas


def save_image(img, path):
    """Save an image ensuring parent directory exists."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, format="PNG")
    print(f"Wrote {path} ({img.width}x{img.height})")


def main():
    repo_root = Path(__file__).resolve().parent.parent
    logo_path = repo_root / "packaging" / "assets" / "wizlight_icon.png"
    android_res = repo_root / "wizlight-android" / "android" / "app" / "src" / "main" / "res"
    branding_dir = repo_root / "wizlight-android" / "assets" / "branding"

    if not logo_path.exists():
        print(f"Error: Base logo not found at {logo_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading official brand logo from {logo_path}...")
    base_logo = load_base_logo(logo_path)

    # Clean up deprecated drawable/ic_launcher_foreground.xml if present
    deprecated_fg = android_res / "drawable" / "ic_launcher_foreground.xml"
    if deprecated_fg.exists():
        try:
            deprecated_fg.unlink()
            print(f"Removed deprecated {deprecated_fg}")
        except Exception as e:
            print(f"Notice: Could not remove {deprecated_fg}: {e}")

    # 1. Legacy Launcher Icons (Square & Round)
    densities = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }

    for folder, size in densities.items():
        square_icon = generate_legacy_launcher_icon(base_logo, size, is_round=False)
        save_image(square_icon, android_res / folder / "ic_launcher.png")

        round_icon = generate_legacy_launcher_icon(base_logo, size, is_round=True)
        save_image(round_icon, android_res / folder / "ic_launcher_round.png")

    # 2. Adaptive Icon Raster Foregrounds (at 108dp sizes)
    adaptive_densities = {
        "mipmap-mdpi": 108,
        "mipmap-hdpi": 162,
        "mipmap-xhdpi": 216,
        "mipmap-xxhdpi": 324,
        "mipmap-xxxhdpi": 432,
    }

    for folder, size in adaptive_densities.items():
        fg_icon = generate_adaptive_foreground(base_logo, size)
        save_image(fg_icon, android_res / folder / "ic_launcher_foreground.png")

    # 3. TV Banners (640x360)
    banner_dir = android_res / "drawable-xhdpi"
    tv_banner = generate_tv_banner(base_logo, 640, 360, is_stop=False)
    save_image(tv_banner, banner_dir / "tv_banner.png")

    tv_banner_stop = generate_tv_banner(base_logo, 640, 360, is_stop=True)
    save_image(tv_banner_stop, banner_dir / "tv_banner_stop.png")

    # 4. In-App Brand Mark (512x512 transparent)
    brand_mark = generate_brand_mark(base_logo, 512)
    save_image(brand_mark, branding_dir / "wizlight_mark.png")

    print("\nAll WizLight brand assets successfully generated!")


if __name__ == "__main__":
    main()
