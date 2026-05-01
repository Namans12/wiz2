"""Create the Windows .ico used by the frozen GUI."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "assets" / "wizlight.ico"


def make_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = size * 0.11
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        [margin, margin * 0.9, size - margin, size * 0.72],
        fill=(64, 170, 255, 110),
    )
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size * 0.055)))

    bulb_box = [margin * 1.25, margin, size - margin * 1.25, size * 0.66]
    draw.ellipse(bulb_box, fill=(74, 160, 236, 255), outline=(235, 247, 255, 230), width=max(1, size // 28))
    draw.ellipse(
        [bulb_box[0] + size * 0.12, bulb_box[1] + size * 0.08, bulb_box[0] + size * 0.27, bulb_box[1] + size * 0.23],
        fill=(255, 255, 255, 110),
    )

    base_left = size * 0.36
    base_right = size * 0.64
    base_top = size * 0.61
    base_bottom = size * 0.84
    draw.rounded_rectangle(
        [base_left, base_top, base_right, base_bottom],
        radius=max(2, size // 22),
        fill=(46, 54, 66, 255),
        outline=(210, 220, 230, 160),
        width=max(1, size // 42),
    )

    for index in range(3):
        y = base_top + size * (0.055 + index * 0.055)
        draw.line(
            [base_left + size * 0.035, y, base_right - size * 0.035, y],
            fill=(145, 158, 174, 230),
            width=max(1, size // 42),
        )

    return image


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    images = [make_icon(size) for size in (16, 24, 32, 48, 64, 128, 256)]
    images[-1].save(OUT, sizes=[(image.width, image.height) for image in images])
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
