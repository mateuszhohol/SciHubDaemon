#!/usr/bin/env python3
"""Generate a scientific icon for Researcher's Best Friend."""

from PIL import Image, ImageDraw, ImageFont
import math
import os

SIZE = 1024
CENTER = SIZE // 2

def create_icon():
    # Create canvas with gradient-like background (deep navy to teal)
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background: rounded rectangle with science-blue gradient effect
    # Base circle
    margin = 40
    draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=180,
        fill=(15, 52, 96),  # deep navy
    )

    # Inner glow
    draw.rounded_rectangle(
        [margin + 8, margin + 8, SIZE - margin - 8, SIZE - margin - 8],
        radius=172,
        fill=(20, 66, 118),
    )

    # --- Draw a magnifying glass over a document ---

    # Document (paper) - white rectangle, slightly tilted feel
    doc_left, doc_top = 280, 180
    doc_right, doc_bot = 620, 720
    # Shadow
    draw.rounded_rectangle(
        [doc_left + 8, doc_top + 8, doc_right + 8, doc_bot + 8],
        radius=20,
        fill=(10, 40, 75),
    )
    # Paper
    draw.rounded_rectangle(
        [doc_left, doc_top, doc_right, doc_bot],
        radius=20,
        fill=(240, 245, 250),
    )

    # Text lines on the document
    line_color = (160, 180, 200)
    line_y_start = 260
    for i in range(8):
        y = line_y_start + i * 48
        width_offset = 40 if i % 3 == 2 else 0  # shorter lines occasionally
        draw.rounded_rectangle(
            [doc_left + 50, y, doc_right - 50 - width_offset, y + 10],
            radius=5,
            fill=line_color,
        )

    # DOI badge on the document
    badge_y = 640
    draw.rounded_rectangle(
        [doc_left + 50, badge_y, doc_left + 220, badge_y + 40],
        radius=10,
        fill=(41, 128, 185),
    )
    # "DOI" text on badge
    try:
        font_doi = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26)
    except:
        font_doi = ImageFont.load_default()
    draw.text((doc_left + 85, badge_y + 6), "DOI", fill=(255, 255, 255), font=font_doi)

    # --- Magnifying glass (overlapping bottom-right of document) ---
    mag_cx, mag_cy = 600, 600
    mag_r = 140

    # Glass circle - outer ring
    ring_width = 18
    draw.ellipse(
        [mag_cx - mag_r, mag_cy - mag_r, mag_cx + mag_r, mag_cy + mag_r],
        fill=None,
        outline=(220, 220, 230),
        width=ring_width,
    )
    # Glass fill (semi-transparent blue tint)
    glass_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glass_draw = ImageDraw.Draw(glass_img)
    glass_draw.ellipse(
        [mag_cx - mag_r + ring_width//2, mag_cy - mag_r + ring_width//2,
         mag_cx + mag_r - ring_width//2, mag_cy + mag_r - ring_width//2],
        fill=(173, 216, 250, 80),
    )
    img = Image.alpha_composite(img, glass_img)
    draw = ImageDraw.Draw(img)

    # Redraw ring on top
    draw.ellipse(
        [mag_cx - mag_r, mag_cy - mag_r, mag_cx + mag_r, mag_cy + mag_r],
        fill=None,
        outline=(200, 210, 225),
        width=ring_width,
    )

    # Handle
    handle_angle = math.radians(45)
    hx1 = mag_cx + int((mag_r + 5) * math.cos(handle_angle))
    hy1 = mag_cy + int((mag_r + 5) * math.sin(handle_angle))
    hx2 = hx1 + int(120 * math.cos(handle_angle))
    hy2 = hy1 + int(120 * math.sin(handle_angle))
    draw.line([(hx1, hy1), (hx2, hy2)], fill=(180, 190, 200), width=28)
    draw.line([(hx1, hy1), (hx2, hy2)], fill=(200, 210, 225), width=20)

    # --- Download arrow (inside magnifying glass) ---
    arrow_cx, arrow_cy = mag_cx, mag_cy - 10
    arrow_color = (41, 128, 185)

    # Vertical bar
    draw.rectangle(
        [arrow_cx - 12, arrow_cy - 50, arrow_cx + 12, arrow_cy + 20],
        fill=arrow_color,
    )
    # Arrow head (triangle pointing down)
    draw.polygon(
        [(arrow_cx - 40, arrow_cy + 15),
         (arrow_cx + 40, arrow_cy + 15),
         (arrow_cx, arrow_cy + 60)],
        fill=arrow_color,
    )
    # Base line
    draw.rounded_rectangle(
        [arrow_cx - 45, arrow_cy + 65, arrow_cx + 45, arrow_cy + 75],
        radius=3,
        fill=arrow_color,
    )

    # Save as PNG
    png_path = os.path.join(os.path.dirname(__file__), "icon.png")
    img.save(png_path, "PNG")
    print(f"Saved: {png_path}")

    # Convert to .icns for macOS
    icns_path = os.path.join(os.path.dirname(__file__), "icon.icns")
    # Create iconset directory
    iconset = os.path.join(os.path.dirname(__file__), "icon.iconset")
    os.makedirs(iconset, exist_ok=True)

    sizes = [16, 32, 64, 128, 256, 512, 1024]
    for s in sizes:
        resized = img.resize((s, s), Image.LANCZOS)
        resized.save(os.path.join(iconset, f"icon_{s}x{s}.png"))
        if s <= 512:
            resized2x = img.resize((s * 2, s * 2), Image.LANCZOS)
            resized2x.save(os.path.join(iconset, f"icon_{s}x{s}@2x.png"))

    os.system(f"iconutil -c icns '{iconset}' -o '{icns_path}'")
    print(f"Saved: {icns_path}")

    # Also save .ico for Windows
    ico_path = os.path.join(os.path.dirname(__file__), "icon.ico")
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico_images = [img.resize(s, Image.LANCZOS) for s in ico_sizes]
    ico_images[0].save(ico_path, format="ICO", sizes=ico_sizes, append_images=ico_images[1:])
    print(f"Saved: {ico_path}")


if __name__ == "__main__":
    create_icon()
