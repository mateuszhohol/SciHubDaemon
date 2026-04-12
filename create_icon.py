#!/usr/bin/env python3
"""Convert icon-new.png into macOS .icns and Windows .ico formats."""

from PIL import Image
import os

BASE_DIR = os.path.dirname(__file__)


def create_icon():
    src_path = os.path.join(BASE_DIR, "icon-new.png")
    img = Image.open(src_path).convert("RGBA")

    # Make square (pad shorter side with transparency)
    w, h = img.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - w) // 2, (side - h) // 2))
    img = square.resize((1024, 1024), Image.LANCZOS)

    # Save canonical PNG
    png_path = os.path.join(BASE_DIR, "icon.png")
    img.save(png_path, "PNG")
    print(f"Saved: {png_path}")

    # --- macOS .icns via iconutil ---
    iconset = os.path.join(BASE_DIR, "icon.iconset")
    os.makedirs(iconset, exist_ok=True)

    sizes = [16, 32, 64, 128, 256, 512, 1024]
    for s in sizes:
        resized = img.resize((s, s), Image.LANCZOS)
        resized.save(os.path.join(iconset, f"icon_{s}x{s}.png"))
        if s <= 512:
            resized2x = img.resize((s * 2, s * 2), Image.LANCZOS)
            resized2x.save(os.path.join(iconset, f"icon_{s}x{s}@2x.png"))

    icns_path = os.path.join(BASE_DIR, "icon.icns")
    os.system(f"iconutil -c icns '{iconset}' -o '{icns_path}'")
    print(f"Saved: {icns_path}")

    # --- Windows .ico ---
    ico_path = os.path.join(BASE_DIR, "icon.ico")
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico_images = [img.resize(s, Image.LANCZOS) for s in ico_sizes]
    ico_images[0].save(ico_path, format="ICO", sizes=ico_sizes, append_images=ico_images[1:])
    print(f"Saved: {ico_path}")


if __name__ == "__main__":
    create_icon()
