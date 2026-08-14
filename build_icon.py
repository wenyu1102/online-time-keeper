# -*- coding: utf-8 -*-
"""生成程序图标 icon.ico（用于 exe 文件图标）"""
import os
from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]


def make_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([1, 1, size - 1, size - 1], fill="#2D7DD2", outline="#FFFFFF")
    cx = cy = size / 2.0
    w = max(2, size // 12)
    d.line([cx, cy, cx, cy - size * 0.30], fill="#FFFFFF", width=w)
    d.line([cx, cy, cx + size * 0.22, cy + size * 0.16], fill="#FFFFFF", width=w)
    d.ellipse([cx - size * 0.07, cy - size * 0.07, cx + size * 0.07, cy + size * 0.07], fill="#FFFFFF")
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    imgs = [make_icon(s) for s in SIZES]
    out = os.path.join(here, "icon.ico")
    imgs[0].save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print("icon written:", out)


if __name__ == "__main__":
    main()
