"""
Draws the MES Viewer icon and writes every size the app and installers need:

    icons/mes_viewer_512.png   master
    icons/mes_viewer_256.png   window / taskbar icon used by the app at run time
    icons/mes_viewer.ico       Windows (16-256 px)
    icons/mes_viewer.icns      macOS

Style matches the other lab apps: a rounded navy tile (#101d3d at the top to #060b1a at the
bottom) with one bright symbol, here a two-photon z-stack in orange (#ff7a59): four imaging
planes stacked in depth, the top one in focus with a few cells, and the converging excitation
cone meeting it at the focal point.

If icons/mes_viewer_512.png is replaced by your own artwork, run
    python icons/make_icons.py --from-png
to make the other sizes from it instead of redrawing.

Needs only Pillow and numpy.
"""

import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4                      # supersampling factor
N = 512 * SS

NAVY_TOP = (0x10, 0x1D, 0x3D)
NAVY_BOTTOM = (0x06, 0x0B, 0x1A)
ORANGE = (0xFF, 0x7A, 0x59)
ORANGE_LIGHT = (0xFF, 0xC4, 0xB0)
WHITE_WARM = (0xFF, 0xF1, 0xEA)


def s(v):
    return int(round(v * SS))


def tile():
    """Rounded square with a vertical navy gradient (RGBA)."""
    t = np.linspace(0, 1, N)[:, None]
    grad = (np.array(NAVY_TOP)[None, None, :] * (1 - t[..., None])
            + np.array(NAVY_BOTTOM)[None, None, :] * t[..., None])
    rgb = np.repeat(grad, N, axis=1).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([s(16), s(16), s(496), s(496)], radius=s(108), fill=255)
    img.putalpha(mask)
    return img, mask


def plane_points(cy, half_w=172, half_h=64, cx=256):
    return [(s(cx), s(cy - half_h)), (s(cx + half_w), s(cy)), (s(cx), s(cy + half_h)), (s(cx - half_w), s(cy))]


def draw():
    base, mask = tile()
    layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # four planes, deepest first; deeper = fainter
    centers = [380, 328, 276, 224]
    fills = [55, 85, 120, 165]
    edges = [150, 190, 225, 255]
    for cy, fa, ea in zip(centers, fills, edges):
        pl = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        d = ImageDraw.Draw(pl)
        pts = plane_points(cy)
        d.polygon(pts, fill=ORANGE + (fa,))
        d.line(pts + [pts[0]], fill=ORANGE + (ea,), width=s(10), joint="curve")
        layer = Image.alpha_composite(layer, pl)

    # cells in the top (focused) plane
    d = ImageDraw.Draw(layer)
    for (x, y, r) in [(194, 218, 15), (318, 236, 13), (246, 254, 10)]:
        d.ellipse([s(x - r), s(y - r), s(x + r), s(y + r)], fill=ORANGE_LIGHT + (255,))

    # converging two-photon excitation cone, meeting the top plane at the focal point
    fx, fy = 256, 224
    cone = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    dc = ImageDraw.Draw(cone)
    dc.polygon([(s(186), s(56)), (s(326), s(56)), (s(fx), s(fy))], fill=ORANGE + (95,))
    dc.line([(s(186), s(56)), (s(fx), s(fy))], fill=ORANGE + (255,), width=s(9))
    dc.line([(s(326), s(56)), (s(fx), s(fy))], fill=ORANGE + (255,), width=s(9))
    layer = Image.alpha_composite(layer, cone)

    # glow + bright focal spot
    glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([s(fx - 40), s(fy - 40), s(fx + 40), s(fy + 40)], fill=ORANGE + (230,))
    glow = glow.filter(ImageFilter.GaussianBlur(s(14)))
    layer = Image.alpha_composite(layer, glow)
    d = ImageDraw.Draw(layer)
    d.ellipse([s(fx - 15), s(fy - 15), s(fx + 15), s(fy + 15)], fill=WHITE_WARM + (255,))

    # keep everything inside the rounded tile
    clipped = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    clipped.paste(layer, (0, 0), Image.composite(layer.getchannel("A"), Image.new("L", (N, N), 0), mask))
    out = Image.alpha_composite(base, clipped)
    return out.resize((512, 512), Image.LANCZOS)


def write_sizes(master):
    master = master.convert("RGBA")
    if master.size != (512, 512):
        master = master.resize((512, 512), Image.LANCZOS)
    master.save(os.path.join(HERE, "mes_viewer_512.png"))
    master.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "mes_viewer_256.png"))
    master.save(os.path.join(HERE, "mes_viewer.ico"),
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    master.save(os.path.join(HERE, "mes_viewer.icns"))


if __name__ == "__main__":
    if "--from-png" in sys.argv:
        write_sizes(Image.open(os.path.join(HERE, "mes_viewer_512.png")))
    else:
        write_sizes(draw())
    print("icons written to", HERE)
