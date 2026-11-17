"""
Synthetic Femtonics-style .mes files with exactly known contents, for tests and the built-in
self-test (``MES-Viewer --selftest <folder>``).

Two layouts are written from the same data:
  * MATLAB v5   (what older MES versions save; read with scipy.io)
  * MATLAB v7.3 (HDF5 with a 512-byte MATLAB header; read with h5py)

Each file holds
  DF0001  z-stack, 2 channels (UG, UR), 6 planes, 64 x 48 px, 0.5 um/px, 2 um z-step,
          frames interleaved UG, UR, UG, UR, ... with images IF0001_0001 ... IF0001_0012
  DF0002  single image, 1 channel (UG), 40 x 30 px

Known structures (pixel coordinates x, y; see TRUTH):
  * green background 100, red background 50
  * a green "cell": x 10-19, y 10-17, +300 in planes 2-4
  * a red-only spot: x 40-47, y 30-37, red +400 in every plane, plus 0.6 x that bleeding into green
  * hot pixels: green plane 0 at (5, 40) = 5000, and both channels of plane 3 at (58, 5) = 5000
"""

import os
import time

import numpy as np

W, H, Z = 64, 48, 6
PX = 0.5          # um per pixel
DZ = 2.0          # um per plane
Z0 = -100.0       # z level of plane 0 (um)
BLEED = 0.6       # fraction of the red spot that leaks into green

TRUTH = dict(
    width=W, height=H, planes=Z, px=PX, dz=DZ, bleed=BLEED,
    bg_green=100, bg_red=50,
    cell=dict(x0=10, x1=19, y0=10, y1=17, planes=(2, 3, 4), amp=300),
    red_spot=dict(x0=40, x1=47, y0=30, y1=37, amp=400),
    hot=[(0, 0, 40, 5), (3, 0, 5, 58), (3, 1, 5, 58)],          # (plane, channel, y, x)
    single=dict(width=40, height=30, value=200, square=(5, 14, 5, 14), square_value=800),
)


def make_volume():
    """The DF0001 stack as (Z, C, Y, X) uint16 (C: 0 = UG, 1 = UR)."""
    t = TRUTH
    v = np.zeros((Z, 2, H, W), np.float64)
    v[:, 0] = t["bg_green"]
    v[:, 1] = t["bg_red"]
    c = t["cell"]
    for z in c["planes"]:
        v[z, 0, c["y0"]:c["y1"] + 1, c["x0"]:c["x1"] + 1] += c["amp"]
    r = t["red_spot"]
    v[:, 1, r["y0"]:r["y1"] + 1, r["x0"]:r["x1"] + 1] += r["amp"]
    v[:, 0, r["y0"]:r["y1"] + 1, r["x0"]:r["x1"] + 1] += BLEED * r["amp"]
    for z, ch, y, x in t["hot"]:
        v[z, ch, y, x] = 5000
    return np.round(v).astype(np.uint16)


def make_single():
    """The DF0002 image as (1, 1, Y, X) uint16."""
    s = TRUTH["single"]
    a = np.full((s["height"], s["width"]), s["value"], np.float64)
    y0, y1, x0, x1 = s["square"]
    a[y0:y1 + 1, x0:x1 + 1] = s["square_value"]
    return a.astype(np.uint16)[None, None]


def _frames(index, vol, channels, context, objective, px, dz):
    """Header frames (one dict per image) in MES order: plane by plane, channels interleaved."""
    Zn, C, Hn, Wn = vol.shape
    frames, images, k = [], {}, 0
    for z in range(Zn):
        for c in range(C):
            k += 1
            name = f"IF{index:04d}_{k:04d}"
            images[name] = vol[z, c].T            # MES/MATLAB layout: Width x Height
            frames.append(dict(
                Type="XY", Context=context, Channel=channels[c], IMAGE=name,
                MeasurementDate="2026.11.17. 12:00:00,000", Comment="synthetic test data",
                Width=float(Wn), Height=float(Hn), WidthStep=px, HeightStep=px,
                WidthUnit="µm", HeightUnit="µm", WidthOrigin=-Wn * px / 2, HeightOrigin=-Hn * px / 2,
                WidthName="x", HeightName="y", Average=2.0, DwellTime=0.005,
                D3Name="z" if Zn > 1 else "", D3Unit="µm", D3Step=dz if Zn > 1 else 0.0,
                D3Size=float(Zn), Zlevel=Z0 + z * dz,
                HardwareState=dict(Objective=objective, Laser="920 nm"),
            ))
    return frames, images


def build():
    """All variables of the synthetic file: {name: header frames (list of dicts) or image array}."""
    out = {}
    f1, i1 = _frames(1, make_volume(), ["UG", "UR"], "Zstack", "Obj_16x", PX, DZ)
    f2, i2 = _frames(2, make_single(), ["UG"], "Photo", "Obj_10x", 1.0, 0.0)
    out["DF0001"], out["DF0002"] = f1, f2
    out.update(i1)
    out.update(i2)
    return out


# ---- MATLAB v5 -----------------------------------------------------------------------------------

def _struct_array(frames):
    fields = list(frames[0].keys())
    arr = np.empty((1, len(frames)), dtype=[(f, object) for f in fields])
    for i, fr in enumerate(frames):
        for f in fields:
            v = fr[f]
            if isinstance(v, dict):                 # nested scalar struct
                sub = np.empty((1, 1), dtype=[(k, object) for k in v])
                for k, x in v.items():
                    sub[0, 0][k] = x
                v = sub
            arr[0, i][f] = v
    return arr


def write_v5(path):
    import scipy.io as sio
    data = {}
    for name, val in build().items():
        data[name] = _struct_array(val) if isinstance(val, list) else val
    sio.savemat(path, data, format="5", do_compression=False, oned_as="row")
    return path


# ---- MATLAB v7.3 (HDF5) --------------------------------------------------------------------------

def _h5_value(f, refs, name, value):
    """Write one MATLAB value into the #refs# group and return its object reference."""
    if isinstance(value, dict):
        g = refs.create_group(name)
        g.attrs["MATLAB_class"] = np.bytes_("struct")
        for k, x in value.items():
            _h5_inline(g, k, x)
        return g.ref
    ds = _h5_inline(refs, name, value)
    return ds.ref


def _h5_inline(group, name, value):
    """Write a char or double value as a dataset named `name` in `group` (MATLAB layout)."""
    if isinstance(value, str):
        codes = np.array([ord(ch) for ch in value] or [0], np.uint16).reshape(-1, 1)
        ds = group.create_dataset(name, data=codes)
        ds.attrs["MATLAB_class"] = np.bytes_("char")
        ds.attrs["MATLAB_int_decode"] = np.int32(2)
        if not value:
            ds.attrs["MATLAB_empty"] = np.uint8(1)
        return ds
    ds = group.create_dataset(name, data=np.array([[float(value)]]))
    ds.attrs["MATLAB_class"] = np.bytes_("double")
    return ds


def write_v73(path):
    import h5py
    data = build()
    with h5py.File(path, "w", userblock_size=512) as f:
        refs = f.create_group("#refs#")
        n = 0
        for name, val in data.items():
            if isinstance(val, list):               # struct array: each field holds object refs
                g = f.create_group(name)
                g.attrs["MATLAB_class"] = np.bytes_("struct")
                for field in val[0].keys():
                    rr = []
                    for fr in val:
                        n += 1
                        rr.append(_h5_value(f, refs, f"r{n:05d}", fr[field]))
                    ds = g.create_dataset(field, data=np.array(rr, dtype=h5py.ref_dtype).reshape(-1, 1),
                                          dtype=h5py.ref_dtype)
                    ds.attrs["MATLAB_class"] = np.bytes_("cell")
            else:                                   # image: MATLAB stores column-major -> transpose
                ds = f.create_dataset(name, data=np.ascontiguousarray(val.T))
                ds.attrs["MATLAB_class"] = np.bytes_("uint16")
    header = ("MATLAB 7.3 MAT-file, Platform: GLNXA64, Created on: "
              + time.strftime("%a %b %d %H:%M:%S %Y") + " HDF5 schema 1.00 .").encode("ascii")
    header = header[:116].ljust(116, b" ") + b"\x00" * 8 + b"\x00\x02" + b"IM"
    with open(path, "r+b") as fh:
        fh.write(header)
    return path


def write_all(folder):
    """Write synthetic_v5.mes and synthetic_v73.mes into `folder`; returns both paths."""
    os.makedirs(folder, exist_ok=True)
    return [write_v5(os.path.join(folder, "synthetic_v5.mes")),
            write_v73(os.path.join(folder, "synthetic_v73.mes"))]


if __name__ == "__main__":
    import sys
    for p in write_all(sys.argv[1] if len(sys.argv) > 1 else "."):
        print(p)
