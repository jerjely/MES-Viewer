#!/usr/bin/env python
"""
MES Viewer — browse Femtonics MES (.mes) files.

  * open one or more .mes files (or a whole folder)
  * overview table of every measurement with key metadata
  * full metadata tree for the selected measurement (with filter)
  * plane viewer with per-channel contrast, composite or single channel
  * z-projection (max / mean / sum / std / median / min) over a chosen z range
  * polygon ROI measurement (area, mean, SD, min, max, median, integrated density)
    on single images, on one plane, on a projection, or across all planes
  * export: ImageJ multi-page TIFF (hyperstack, calibrated, green/magenta LUTs),
    one TIFF per channel, projection TIFF, PNG of the current view,
    batch export of every measurement, overview CSV, metadata JSON, results CSV
  * 3D view: napari (if installed) in a separate process, otherwise a built-in
    rotating max-projection renderer and orthogonal (XY / XZ / YZ) views

Usage:
    python mes_viewer.py [file1.mes file2.mes ...]
    python mes_viewer.py --version
    python mes_viewer.py --selftest <folder>     # synthetic .mes files -> <folder>/selftest.txt, exit code

Requirements: numpy, scipy, matplotlib, tifffile, h5py   (optional: napari[all])
"""

import os
import re
import sys
import csv
import json
import queue
import tempfile
import threading
import traceback
import subprocess
from collections import OrderedDict

import numpy as np
import scipy.io as sio
from scipy import ndimage

try:                                    # rewritten from the git tag by the release workflow
    from version import APP_VERSION as __version__
except Exception:                       # pragma: no cover
    __version__ = "dev"

FROZEN = bool(getattr(sys, "frozen", False))          # running from the installed app (PyInstaller)
APP_ID = "io.github.jerjely.mesviewer"


def resource_path(*parts):
    """A file shipped with the app (icons, README): next to this script, or inside the frozen bundle."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def napari_available():
    """napari is optional and never part of the installers; it is only used when running from source."""
    if FROZEN:
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("napari") is not None
    except Exception:
        return False

# Colours used for known MES channel names; anything else gets the fallback list
CH_COLORS = {"UG": (0.0, 1.0, 0.0), "UR": (1.0, 0.0, 1.0),
             "Camera": (1.0, 1.0, 1.0)}
FALLBACK_COLORS = [(0, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 0), (1, 1, 1)]

PROJ_METHODS = ["max", "mean", "sum", "std", "median", "min"]


# =============================================================================
#  Reading .mes files
# =============================================================================

class S:
    """Minimal stand-in for scipy's mat_struct (used for the HDF5 reader)."""

    def __init__(self, d):
        self.__dict__.update(d)
        self._fieldnames = list(d.keys())


def _is_struct(x):
    return hasattr(x, "_fieldnames")


def _get(e, name, default=None):
    if e is None:
        return default
    if _is_struct(e):
        return getattr(e, name, default) if name in e._fieldnames else default
    if isinstance(e, dict):
        return e.get(name, default)
    return default


def _scalar(x, default=None):
    try:
        if x is None:
            return default
        a = np.asarray(x)
        if a.size == 0:
            return default
        v = a.ravel()[0]
        return v.item() if hasattr(v, "item") else v
    except Exception:
        return default


def _float(x, default=np.nan):
    v = _scalar(x, None)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _str(x):
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    a = np.asarray(x)
    if a.size == 0:
        return ""
    if a.dtype.kind in "US":
        return " ".join(str(v) for v in a.ravel())
    return str(_scalar(x))


def _h5_read(f, obj):
    """Best-effort conversion of a MATLAB v7.3 (HDF5) object to python."""
    import h5py
    cls = obj.attrs.get("MATLAB_class", b"")
    if isinstance(cls, bytes):
        cls = cls.decode()
    if isinstance(obj, h5py.Group):
        fields = {k: _h5_read(f, obj[k]) for k in obj.keys()}
        lens = {len(v) for v in fields.values() if isinstance(v, list)}
        if fields and len(lens) == 1 and all(isinstance(v, list) for v in fields.values()):
            n = lens.pop()
            if n > 1:  # struct array -> list of structs
                return [S({k: v[i] for k, v in fields.items()}) for i in range(n)]
        return S(fields)
    if obj.attrs.get("MATLAB_empty", 0):
        return np.array([])
    data = obj[()]
    if obj.dtype == h5py.ref_dtype:
        out = [_h5_read(f, f[r]) for r in np.asarray(data).ravel()]
        return out[0] if len(out) == 1 else out
    if cls == "char":
        return "".join(chr(c) for c in np.asarray(data).T.ravel() if c)
    data = np.asarray(data)
    return data.T if data.ndim >= 2 else data


class Measurement:
    """One MES measurement unit (DFxxxx header + its IFxxxx_yyyy images)."""

    def __init__(self, mfile, key, frames):
        self.file = mfile
        self.key = key
        self.frames = frames
        e = frames[0]
        self.hdr = e
        self.index = int(re.sub(r"\D", "", key) or 0)
        self.type = _str(_get(e, "Type"))
        self.context = _str(_get(e, "Context"))
        self.date = _str(_get(e, "MeasurementDate"))
        self.objective = _str(_get(_get(e, "HardwareState"), "Objective"))
        self.comment = _str(_get(e, "Comment"))
        self.width = int(_float(_get(e, "Width"), 0) or 0)
        self.height = int(_float(_get(e, "Height"), 0) or 0)
        self.px = _float(_get(e, "WidthStep"), 1.0) or 1.0
        self.py = _float(_get(e, "HeightStep"), 1.0) or 1.0
        self.xunit = _str(_get(e, "WidthUnit")) or "px"
        self.yunit = _str(_get(e, "HeightUnit")) or "px"
        self.x0 = _float(_get(e, "WidthOrigin"), 0.0)
        self.y0 = _float(_get(e, "HeightOrigin"), 0.0)
        self.average = _scalar(_get(e, "Average"), "")
        self.dwell = _scalar(_get(e, "DwellTime"), "")
        self.d3name = _str(_get(e, "D3Name"))
        self.d3unit = _str(_get(e, "D3Unit")) or "µm"

        # group frames by channel (MES interleaves them: UG, UR, UG, UR, ...)
        order, byc = [], {}
        for fr in frames:
            ch = _str(_get(fr, "Channel")) or "ch"
            if ch not in byc:
                byc[ch] = []
                order.append(ch)
            byc[ch].append(fr)
        self.channels = order
        self.nplanes = min(len(v) for v in byc.values())
        self.image_names = [[(_str(_get(byc[ch][i], "IMAGE")) or _str(_get(byc[ch][i], "ImageName")))
                             for ch in order] for i in range(self.nplanes)]
        self.zlevels = np.array([_float(_get(byc[order[0]][i], "Zlevel"))
                                 for i in range(self.nplanes)], float)
        self.is_stack = self.nplanes > 1
        self.axis3 = self.d3name or ("z" if "stack" in self.context.lower() else "plane")
        self.dz = _float(_get(e, "D3Step"))
        if (not np.isfinite(self.dz) or self.dz == 0) and self.is_stack:
            d = np.diff(self.zlevels)
            d = d[np.isfinite(d)]
            self.dz = float(np.median(np.abs(d))) if d.size else 1.0
        if not np.isfinite(self.dz) or self.dz == 0:
            self.dz = 1.0

    # ---- convenience -------------------------------------------------------
    @property
    def label(self):
        return f"{self.file.name} · {self.key}"

    @property
    def is_spatial(self):
        return self.xunit in ("µm", "um", "micron") and self.yunit in ("µm", "um", "micron")

    def color(self, ch_index):
        ch = self.channels[ch_index]
        return CH_COLORS.get(ch, FALLBACK_COLORS[ch_index % len(FALLBACK_COLORS)])

    def summary(self):
        fov = (f"{self.width * self.px:.0f}×{self.height * self.py:.0f}"
               if self.is_spatial else "")
        return OrderedDict([
            ("File", self.file.name),
            ("Key", self.key),
            ("Type", self.type),
            ("Context", self.context),
            ("Date", self.date),
            ("Objective", self.objective),
            ("Channels", ",".join(self.channels)),
            ("Size (W×H)", f"{self.width}×{self.height}"),
            ("Planes", self.nplanes),
            ("Step", f"{self.dz:g} {self.d3unit}" if self.is_stack else ""),
            ("Depth", f"{(self.nplanes - 1) * self.dz:g}" if self.is_stack else ""),
            ("Pixel", f"{self.px:.4g} {self.xunit}"),
            ("FOV (µm)", fov),
            ("Avg", self.average),
            ("Dwell", self.dwell),
            ("Z first", f"{self.zlevels[0]:.1f}" if np.isfinite(self.zlevels[0]) else ""),
            ("Comment", self.comment),
        ])

    def info_dict(self):
        d = dict(self.summary())
        d.update({"px_x": self.px, "px_y": self.py, "x_unit": self.xunit, "y_unit": self.yunit,
                  "x_origin": self.x0, "y_origin": self.y0, "dz": self.dz,
                  "zlevels": [None if not np.isfinite(z) else float(z) for z in self.zlevels],
                  "source_path": self.file.path})
        d["Planes"] = int(d["Planes"])
        return d

    # ---- data --------------------------------------------------------------
    def load(self):
        """Return volume as (Z, C, Y, X) in the native dtype."""
        flat = [n for row in self.image_names for n in row]
        data = self.file.load_vars(flat)
        missing = [n for n in flat if n not in data]
        if missing:
            raise KeyError(f"Image variable(s) not found in file: {missing[:5]}")
        planes = []
        for row in self.image_names:
            chans = []
            for n in row:
                a = np.asarray(data[n])
                if a.ndim != 2:
                    a = np.squeeze(a)
                # MES stores images x-first (Width × Height) -> transpose to (Y, X)
                if a.shape == (self.height, self.width) and a.shape != (self.width, self.height):
                    pass
                else:
                    a = a.T
                chans.append(a)
            planes.append(np.stack(chans))
        return np.stack(planes)


class MesFile:
    HEADER_RE = re.compile(r"^[Dd][Ff]\d{3,}$")

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(path)
        with open(path, "rb") as fh:
            head = fh.read(128)
        if not head.startswith(b"MATLAB"):
            raise ValueError(f"{self.name}: not a MATLAB-based .mes file")
        self.v73 = b"MATLAB 7.3" in head
        self.measurements = []
        self._read_headers()

    def _read_headers(self):
        if self.v73:
            import h5py
            with h5py.File(self.path, "r") as f:
                keys = [k for k in f.keys() if self.HEADER_RE.match(k)]
                hdr = {k: _h5_read(f, f[k]) for k in keys}
        else:
            keys = [n for n, _, _ in sio.whosmat(self.path) if self.HEADER_RE.match(n)]
            hdr = sio.loadmat(self.path, variable_names=keys, squeeze_me=True,
                              struct_as_record=False)
        for k in sorted(keys, key=lambda s: int(re.sub(r"\D", "", s) or 0)):
            v = hdr[k]
            frames = v if isinstance(v, list) else list(np.atleast_1d(v).ravel())
            if frames:
                self.measurements.append(Measurement(self, k, frames))

    def load_vars(self, names):
        if self.v73:
            import h5py
            out = {}
            with h5py.File(self.path, "r") as f:
                for n in names:
                    if n in f:
                        out[n] = np.asarray(f[n][()]).T
            return out
        return sio.loadmat(self.path, variable_names=list(names))


# =============================================================================
#  Image helpers (no GUI)
# =============================================================================

def project(vol, method="max", z0=0, z1=None):
    """vol (Z,C,Y,X) -> (C,Y,X) float32."""
    sub = vol[z0:(z1 + 1 if z1 is not None else None)]
    if method == "max":
        return sub.max(0).astype(np.float32)
    if method == "min":
        return sub.min(0).astype(np.float32)
    if method == "mean":
        return sub.mean(0, dtype=np.float64).astype(np.float32)
    if method == "sum":
        return sub.sum(0, dtype=np.float64).astype(np.float32)
    if method == "std":
        return sub.std(0, dtype=np.float64).astype(np.float32)
    if method == "median":
        return np.median(sub, axis=0).astype(np.float32)
    raise ValueError(method)


def auto_limits(a, lo_pct, hi_pct):
    step = max(1, max(a.shape) // 512)
    s = a[::step, ::step]
    lo, hi = np.percentile(s, [lo_pct, hi_pct])
    if hi <= lo:
        hi = lo + 1
    return float(lo), float(hi)


def to_rgb(img_c, colors, visible, limits, gamma=1.0):
    C, H, W = img_c.shape
    rgb = np.zeros((H, W, 3), np.float32)
    for c in range(C):
        if not visible[c]:
            continue
        lo, hi = limits[c]
        n = np.clip((img_c[c].astype(np.float32) - lo) / (hi - lo), 0, 1)
        if gamma != 1.0:
            n = n ** gamma
        rgb += n[..., None] * np.asarray(colors[c], np.float32)
    return np.clip(rgb, 0, 1)


def polygon_mask(verts, shape):
    from matplotlib.path import Path as MplPath
    H, W = shape
    v = np.asarray(verts, float)
    x0 = max(int(np.floor(v[:, 0].min())), 0)
    x1 = min(int(np.ceil(v[:, 0].max())), W - 1)
    y0 = max(int(np.floor(v[:, 1].min())), 0)
    y1 = min(int(np.ceil(v[:, 1].max())), H - 1)
    m = np.zeros(shape, bool)
    if x1 < x0 or y1 < y0:
        return m
    yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
    inside = MplPath(v).contains_points(np.c_[xx.ravel(), yy.ravel()])
    m[y0:y1 + 1, x0:x1 + 1] = inside.reshape(yy.shape)
    return m


def roi_geometry(verts, px, py):
    v = np.asarray(verts, float) * [px, py]
    x, y = v[:, 0], v[:, 1]
    area_poly = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    perim = float(np.sum(np.hypot(np.diff(np.r_[x, x[0]]), np.diff(np.r_[y, y[0]]))))
    return area_poly, perim


def roi_stats(img2d, mask):
    vals = img2d[mask].astype(np.float64)
    if vals.size == 0:
        return dict(n=0, mean=np.nan, sd=np.nan, min=np.nan, max=np.nan, median=np.nan, intden=np.nan)
    return dict(n=int(vals.size), mean=vals.mean(), sd=vals.std(ddof=1) if vals.size > 1 else 0.0,
                min=vals.min(), max=vals.max(), median=float(np.median(vals)), intden=vals.sum())


def make_lut(color):
    ramp = np.arange(256, dtype=np.float64)
    return np.stack([np.round(ramp * c) for c in color]).astype(np.uint8)


def _imagej_unit(u):
    return "micron" if u in ("µm", "um", "micron") else (u or "pixel")


def write_tiff(path, arr_zcyx, m, channels=None, info_extra=None, names=None, colors=None):
    """Write a (Z,C,Y,X) array as an ImageJ hyperstack with calibration.

    names / colors: per-channel labels and LUT colours (default: the measurement's own
    channels, selected by `channels`). Used when derived channels are included."""
    import tifffile
    arr = np.asarray(arr_zcyx)
    if arr.dtype not in (np.uint8, np.uint16, np.float32):
        arr = arr.astype(np.float32)
    ch_idx = list(range(arr.shape[1])) if channels is None else channels
    if names is None:
        names = [m.channels[c] for c in ch_idx]
    if colors is None:
        colors = [m.color(c) for c in ch_idx]
    meta = {"axes": "ZCYX", "unit": _imagej_unit(m.xunit),
            "mode": "composite" if arr.shape[1] > 1 else "grayscale",
            "LUTs": [make_lut(col) for col in colors],
            "Labels": [f"{names[c]} z{z}" for z in range(arr.shape[0]) for c in range(arr.shape[1])]}
    if arr.shape[0] > 1:
        meta["spacing"] = float(m.dz)
    info = m.info_dict()
    if info_extra:
        info.update(info_extra)
    meta["Info"] = json.dumps(info, indent=1, default=str)
    tifffile.imwrite(path, arr, imagej=True, resolution=(1.0 / m.px, 1.0 / m.py), metadata=meta)


def channel_offsets(vol, chans, pct):
    """Per-channel background offset = `pct` percentile of the whole volume (subsampled)."""
    step = max(1, vol.shape[0] // 30)
    return [float(np.percentile(vol[::step, c, ::4, ::4], pct)) for c in chans]


def subtract_channels(vol, a, b, k, offsets=(0.0, 0.0), clip=False):
    """(A - offA) - k * (B - offB) for every plane. vol (Z,C,Y,X) -> (Z,Y,X) float32."""
    out = vol[:, a].astype(np.float32) - np.float32(offsets[0])
    out -= np.float32(k) * (vol[:, b].astype(np.float32) - np.float32(offsets[1]))
    if clip:
        np.maximum(out, 0, out=out)
    return out


def k_from_mask(vol, a, b, mask, offsets=(0.0, 0.0)):
    """Scale factor that best removes channel B from channel A inside `mask`
    (least-squares slope through the origin over all planes, offsets removed).
    Draw the mask on something that should vanish after subtraction,
    e.g. an autofluorescent spot visible in both channels."""
    A = vol[:, a][:, mask].astype(np.float64).ravel() - offsets[0]
    B = vol[:, b][:, mask].astype(np.float64).ravel() - offsets[1]
    den = float(np.sum(B * B))
    if A.size == 0 or den <= 0:
        return np.nan
    return float(np.sum(A * B) / den)


NR_METHODS = ["none", "Gaussian 2D (per plane)", "Gaussian 3D", "Median 2D (per plane)", "Median 3D"]
OUTLIER_MODES = ["bright", "dark", "both"]


def _median_stack(v, k, kz=1):
    """Median filter of a (Z,C,Y,X) float32 stack, size (kz, 1, k, k), parallel over planes /
    z-chunks (scipy releases the GIL, so this scales with CPU cores). Same result as one call."""
    from concurrent.futures import ThreadPoolExecutor
    Z, C = v.shape[:2]
    out = np.empty_like(v)
    jobs = []
    if kz == 1:
        for z in range(Z):
            for c in range(C):
                jobs.append((z, c))

        def run(j):
            z, c = j
            out[z, c] = ndimage.median_filter(v[z, c], size=k)
    else:
        h = kz // 2
        step = max(1, int(np.ceil(Z / max(1, (os.cpu_count() or 2)))))
        for c in range(C):
            for z0 in range(0, Z, step):
                jobs.append((c, z0, min(Z, z0 + step)))

        def run(j):
            c, z0, z1 = j
            a, b = max(0, z0 - h), min(Z, z1 + h)
            if a == 0 and b == Z:
                res = ndimage.median_filter(v[:, c], size=(kz, k, k))
            else:
                slab = v[a:b, c]
                # pad only at the true stack ends (reflect), interior edges use real neighbours
                pre = h - (z0 - a)
                post = h - (b - z1)
                if pre or post:
                    slab = np.pad(slab, ((pre, post), (0, 0), (0, 0)), mode="symmetric")
                res = ndimage.median_filter(slab, size=(kz, k, k))[h:-h or None]
                out[z0:z1, c] = res[: z1 - z0]
                return
            out[:, c] = res
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        list(ex.map(run, jobs))
    return out


def noise_sd(vol):
    """Robust noise SD for every plane and channel -> (Z, C).
    Uses differences between horizontally neighbouring pixels (MAD / √2), so it needs no
    smoothing and is cheap; computed on every other row of the full plane."""
    v = np.asarray(vol)
    Z, C = v.shape[:2]
    out = np.ones((Z, C), np.float64)
    for z in range(Z):
        for c in range(C):
            rows = v[z, c, ::2].astype(np.float32)
            d = (rows[:, 1:] - rows[:, :-1]).ravel()
            mad = float(np.median(np.abs(d - np.median(d))))
            sd = 1.4826 * mad / np.sqrt(2)
            if sd <= 0:
                sd = float(d.std()) / np.sqrt(2) or 1.0
            out[z, c] = sd
    return out


def remove_outliers(vol, radius=2, threshold=4.0, which="bright", sd=None):
    """ImageJ-style 'Remove Outliers', per plane and channel: a pixel is replaced by the median of
    its (2r+1)×(2r+1) neighbourhood if it deviates from that median by more than
    threshold × (noise SD of that plane and channel, see noise_sd()).
    Removes hot pixels / specks smaller than the radius; leaves everything else untouched.
    sd: optional precomputed (Z, C) noise SDs (used by the preview so crops give identical results).
    Returns (filtered float32 stack, fraction of pixels replaced)."""
    v = np.asarray(vol, dtype=np.float32)
    k = 2 * max(1, int(radius)) + 1
    med = _median_stack(v, k)
    if sd is None:
        sd = noise_sd(v)
    thr = (threshold * np.asarray(sd, np.float32))[:, :, None, None]
    d = v - med
    if which == "bright":
        msk = d > thr
    elif which == "dark":
        msk = d < -thr
    else:
        msk = np.abs(d) > thr
    out = np.where(msk, med, v)
    return out, float(msk.mean())


def denoise(vol, method, sigma_xy=1.0, sigma_z=1.0, size=3):
    """Smoothing of a (Z,C,Y,X) volume, channel by channel -> float32.
    sigma_xy / size in pixels, sigma_z in planes. 3D methods fall back to 2D for single planes."""
    v = np.asarray(vol, dtype=np.float32)
    Z = v.shape[0]
    if method.startswith("Gaussian"):
        sz = sigma_z if (method == "Gaussian 3D" and Z > 1) else 0
        return ndimage.gaussian_filter(v, (sz, 0, sigma_xy, sigma_xy))
    if method.startswith("Median"):
        k = max(1, int(size)) | 1                       # odd kernel
        kz = 3 if (method == "Median 3D" and Z > 2) else 1
        return _median_stack(v, k, kz)
    return v


def apply_nr(vol, p, sd=None):
    """Full noise-reduction pipeline: optional outlier removal, then smoothing.
    p: dict(method, sigma_xy, sigma_z, size, outliers, out_radius, out_threshold, out_which).
    Returns (float32 stack, info string)."""
    v = np.asarray(vol, dtype=np.float32)
    parts = []
    if p.get("outliers"):
        v, frac = remove_outliers(v, p["out_radius"], p["out_threshold"], p["out_which"], sd=sd)
        parts.append(f"{frac * 100:.2f}% pixels replaced")
    if p.get("method", "none") != "none":
        v = denoise(v, p["method"], p["sigma_xy"], p["sigma_z"], p["size"])
    return v, ", ".join(parts)


def _nr_margin(p):
    """Pixels of context a filter needs around a crop so the crop's result is exact."""
    mg = 2
    if p.get("outliers"):
        mg += int(p["out_radius"])
    meth = p.get("method", "none")
    if meth.startswith("Gaussian"):
        mg += int(4 * p["sigma_xy"] + 0.5)
    elif meth.startswith("Median"):
        mg += (int(p["size"]) | 1) // 2
    return mg


def preview_filter(base, p, z=None, proj=None, crop=None):
    """Fast noise-reduction preview of only what is on screen.

    base: (Z,C,Y,X) raw stack.  z: plane index -> only the planes needed around it are filtered
    (exact result for that plane).  proj=(method, z0, z1): the projection is filtered as a 2D image
    (approximation: the full 'Apply' filters planes before projecting).
    crop=(y0, y1, x0, x1): only this region (plus the margin the filter needs) is computed.
    Returns the full (C,Y,X) float32 image with the filtered region pasted into the raw one."""
    Z, C, Y, X = base.shape
    y0, y1, x0, x1 = crop if crop else (0, Y, 0, X)
    mg = _nr_margin(p)
    a0, a1 = max(0, y0 - mg), min(Y, y1 + mg)
    b0, b1 = max(0, x0 - mg), min(X, x1 + mg)
    q = dict(p)
    if proj is not None:
        meth, z0, z1 = proj
        full = project(base, meth, z0, z1)
        sub = full[None, :, a0:a1, b0:b1]
        sd = noise_sd(full[None]) if q.get("outliers") else None
        q["method"] = {"Gaussian 3D": "Gaussian 2D (per plane)",
                       "Median 3D": "Median 2D (per plane)"}.get(q["method"], q["method"])
        k = 0
    else:
        full = base[z].astype(np.float32)
        hz = 0
        if q.get("method") == "Gaussian 3D":
            hz = int(4 * q["sigma_z"] + 0.5)
        elif q.get("method") == "Median 3D":
            hz = 1
        s0, s1 = max(0, z - hz), min(Z, z + hz + 1)
        if q.get("method") == "Median 3D" and Z > 2 and s1 - s0 < 3:
            # first / last plane: still give the filter 3 planes, as the full stack does
            s0 = max(0, min(s0, Z - 3))
            s1 = s0 + 3
        sub = base[s0:s1, :, a0:a1, b0:b1]
        sd = noise_sd(base[s0:s1]) if q.get("outliers") else None
        k = z - s0
    res, _info = apply_nr(sub, q, sd=sd)
    out = np.array(full, dtype=np.float32, copy=True)
    out[:, y0:y1, x0:x1] = res[k, :, y0 - a0:y1 - a0, x0 - b0:x1 - b0]
    return out


def nr_description(p, m, Z):
    parts = []
    if p.get("outliers"):
        parts.append(f"outliers ({p['out_which']}, r={p['out_radius']:g} px, >{p['out_threshold']:g} SD)")
    meth = p.get("method", "none")
    if meth.startswith("Gaussian"):
        d = f"Gaussian σ={p['sigma_xy']:g} px ({p['sigma_xy'] * m.px:.2g} {m.xunit})"
        if meth == "Gaussian 3D" and Z > 1:
            d += f", σz={p['sigma_z']:g} planes ({p['sigma_z'] * m.dz:.2g} {m.d3unit})"
        parts.append(d)
    elif meth.startswith("Median"):
        k = int(p["size"]) | 1
        parts.append(f"median {k}×{k} px" + (" ×3 planes" if meth == "Median 3D" and Z > 2 else ""))
    return " + ".join(parts)


def nr_enabled(p):
    return bool(p.get("outliers")) or p.get("method", "none") != "none"


def roi_traces(chan_vol, masks):
    """Mean of each mask in every plane: chan_vol (Z,Y,X) -> (n_rois, Z)."""
    out = np.full((len(masks), chan_vol.shape[0]), np.nan)
    for i, mk in enumerate(masks):
        if mk.any():
            out[i] = chan_vol[:, mk].astype(np.float64).mean(1)
    return out


def delta_f_over_f(trace, method="first", n=3, pct=10.0):
    """ΔF/F = (F - F0) / F0.  F0: 'first' = mean of the first n points,
    'percentile' = pct-th percentile of the trace, 'min' = minimum."""
    t = np.asarray(trace, float)
    if method == "first":
        f0 = np.nanmean(t[:max(1, int(n))])
    elif method == "percentile":
        f0 = np.nanpercentile(t, pct)
    else:
        f0 = np.nanmin(t)
    if not np.isfinite(f0) or f0 <= 0:
        return np.full_like(t, np.nan), f0
    return (t - f0) / f0, f0


def safe_name(s):
    return re.sub(r"[^\w\-.]+", "_", s).strip("_")


def default_stem(m):
    return safe_name(f"{os.path.splitext(m.file.name)[0]}_{m.key}_{m.context or m.type}"
                     f"_{m.objective}".rstrip("_"))


def struct_to_jsonable(x, depth=0, max_depth=8):
    if depth > max_depth:
        return "…"
    if _is_struct(x):
        return {k: struct_to_jsonable(getattr(x, k), depth + 1) for k in x._fieldnames}
    if isinstance(x, (list, tuple)):
        return [struct_to_jsonable(v, depth + 1) for v in x]
    if isinstance(x, np.ndarray):
        if x.dtype == object:
            return [struct_to_jsonable(v, depth + 1) for v in x.ravel()]
        if x.size > 1000:
            return f"<array {x.shape} {x.dtype}>"
        return x.tolist()
    if isinstance(x, (np.generic,)):
        return x.item()
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)


# =============================================================================
#  napari launcher (runs in a separate process: `mes_viewer.py --napari ...`)
# =============================================================================

def run_napari(npy_path, meta_path):
    try:
        import napari
        vol = np.load(npy_path)
        with open(meta_path) as fh:
            meta = json.load(fh)
        v = napari.Viewer(title=meta.get("title", "MES 3D"), ndisplay=3)
        names = meta["channels"]
        cols = meta.get("colors") or [(1, 1, 1)] * len(names)
        lims = []
        for c in range(vol.shape[1]):
            lims.append(list(np.percentile(vol[:, c, ::4, ::4], [0.5, 99.8]).astype(float)))
        kw = dict(channel_axis=1, name=names,
                  colormap=[napari.utils.Colormap([[0, 0, 0, 1], list(c) + [1]], name=f"mes_{i}_{n}")
                            for i, (c, n) in enumerate(zip(cols, names))],
                  scale=(meta["dz"], meta["py"], meta["px"]),        # µm per voxel (z, y, x)
                  blending="additive", contrast_limits=lims, rendering="mip")
        try:   # napari >= 0.6 understands physical units per axis
            v.add_image(vol, units=("um", "um", "um"), **kw)
        except TypeError:
            v.add_image(vol, **kw)
        v.scale_bar.visible = True
        v.scale_bar.unit = "um"          # data coordinates are µm because of `scale`
        try:
            v.scale_bar.ticks = False
            v.scale_bar.box = True
        except Exception:
            pass
        v.axes.visible = True            # x/y/z axis arrows
        v.dims.axis_labels = ("z (µm)", "y (µm)", "x (µm)")
        napari.run()
    finally:
        for p in (npy_path, meta_path):
            try:
                os.remove(p)
            except OSError:
                pass


# =============================================================================
#  GUI
# =============================================================================

def build_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.widgets import PolygonSelector
    from matplotlib.patches import Polygon as MplPolygon
    from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar

    def nice_bar(length_units):
        """pick a round scale bar length close to 1/5 of the field width."""
        target = length_units / 5
        if target <= 0:
            return 1
        p = 10 ** np.floor(np.log10(target))
        for k in (5, 2, 1):
            if k * p <= target:
                return k * p
        return p

    # -------------------------------------------------------------------------
    class Viewer3D(tk.Toplevel):
        """Built-in 3D fallback: rotating MIP + orthogonal views."""

        def __init__(self, master, m, vol, colors=None, raw=None, nr=None, nr_desc=""):
            """vol: volume to show first (may already be noise-reduced);
            raw: unfiltered volume used when noise reduction is changed in this window;
            nr: the noise-reduction settings that produced `vol` (dict for apply_nr)."""
            super().__init__(master)
            self.title(f"3D view — {m.label}")
            self.geometry("1150x850")
            self.m = m
            self.raw = vol if raw is None else raw
            C = vol.shape[1]
            self.colors = list(colors) if colors else [m.color(c) for c in range(C)]
            self._busy = False
            self._build_nr_bar(nr or {}, nr_desc)
            self._prepare(vol)

            nb = ttk.Notebook(self)
            nb.pack(fill="both", expand=True)
            self._build_mip_tab(nb)
            self._build_ortho_tab(nb)
            self.after(100, self.render_mip)

        # ---- noise reduction inside the 3D window -----------------------------
        def _build_nr_bar(self, nr, desc):
            bar = ttk.LabelFrame(self, text="Noise reduction (this 3D view)", padding=4)
            bar.pack(side="top", fill="x", padx=6, pady=(4, 0))
            self.v_out = tk.BooleanVar(value=bool(nr.get("outliers")))
            self.v_out_r = tk.StringVar(value=f"{nr.get('out_radius', 2):g}")
            self.v_out_t = tk.StringVar(value=f"{nr.get('out_threshold', 4):g}")
            self.v_out_w = tk.StringVar(value=nr.get("out_which", "bright"))
            self.v_meth = tk.StringVar(value=nr.get("method", "none"))
            self.v_sx = tk.StringVar(value=f"{nr.get('sigma_xy', 1.0):g}")
            self.v_sz = tk.StringVar(value=f"{nr.get('sigma_z', 1.0):g}")
            self.v_k = tk.StringVar(value=f"{nr.get('size', 3):g}")
            ttk.Checkbutton(bar, text="remove outliers", variable=self.v_out).pack(side="left")
            for lbl, var, w in (("r px", self.v_out_r, 3), ("> SD", self.v_out_t, 4)):
                ttk.Label(bar, text=lbl).pack(side="left", padx=(4, 0))
                ttk.Entry(bar, textvariable=var, width=w).pack(side="left")
            ttk.Combobox(bar, textvariable=self.v_out_w, values=OUTLIER_MODES, state="readonly",
                         width=6).pack(side="left", padx=3)
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
            ttk.Combobox(bar, textvariable=self.v_meth, values=NR_METHODS, state="readonly",
                         width=22).pack(side="left")
            for lbl, var in (("σ xy", self.v_sx), ("σ z", self.v_sz), ("median px", self.v_k)):
                ttk.Label(bar, text=lbl).pack(side="left", padx=(6, 0))
                ttk.Entry(bar, textvariable=var, width=4).pack(side="left")
            ttk.Button(bar, text="Apply", command=self.apply_nr).pack(side="left", padx=8)
            self.nr_status = ttk.Label(self, text="  " + (f"active: {desc}" if desc else "noise reduction: none"),
                                       foreground="#666")
            self.nr_status.pack(side="top", fill="x", padx=8)

        def _nr_dict(self):
            f = lambda v: float(v.get().replace(",", "."))  # noqa: E731
            return dict(outliers=self.v_out.get(), out_radius=f(self.v_out_r), out_threshold=f(self.v_out_t),
                        out_which=self.v_out_w.get(), method=self.v_meth.get(), sigma_xy=f(self.v_sx),
                        sigma_z=f(self.v_sz), size=int(f(self.v_k)))

        def apply_nr(self):
            if self._busy:
                return
            try:
                p = self._nr_dict()
            except ValueError:
                self.nr_status.config(text="  invalid number")
                return
            desc = nr_description(p, self.m, self.raw.shape[0]) if nr_enabled(p) else ""
            self.nr_status.config(text="  " + f"filtering… {desc}" if desc else "removing filter…")
            self._busy = True
            res = {}

            def work():
                try:
                    if nr_enabled(p):
                        v, info = apply_nr(self.raw, p)
                    else:
                        v, info = self.raw, ""
                    res["v"], res["info"] = v, info
                    self._prepare(v, compute_only=True, out=res)
                except Exception as ex:  # noqa
                    res["err"] = ex

            th = threading.Thread(target=work, daemon=True)
            th.start()

            def poll():
                if th.is_alive():
                    self.after(80, poll)
                    return
                self._busy = False
                if "err" in res:
                    self.nr_status.config(text="  " + f"error: {res['err']}")
                    return
                self._prepare(res["v"], prepared=res)
                txt = f"active: {desc}" if desc else "none"
                if res.get("info"):
                    txt += f"  ({res['info']})"
                self.nr_status.config(text="  " + txt)
                self.title(f"3D view — {self.m.label}" + (f"   [{desc}]" if desc else ""))
                self.render_ortho()
                self.render_mip()

            poll()

        def _prepare(self, vol, compute_only=False, out=None, prepared=None):
            """Compute display limits and the isotropic render volume for `vol`.
            compute_only: just fill `out` (safe in a worker thread); prepared: apply a filled dict."""
            if prepared is not None:
                for k in ("vol", "lims", "plane_lims", "iso", "voxel", "zscale", "bar_um"):
                    setattr(self, k, prepared[k])
                return
            m = self.m
            Z, C, Y, X = vol.shape
            d = {"vol": vol}
            # limits for the projection renderer (from the MIP) and for single-plane views (from all voxels)
            d["lims"] = [auto_limits(vol[:, c].max(0), 0.5, 99.8) for c in range(C)]
            step = max(1, Z // 20)
            d["plane_lims"] = []
            for c in range(C):
                s = vol[::step, c, ::4, ::4]
                lo, hi = np.percentile(s, [1, 99.9])
                d["plane_lims"].append((float(lo), float(max(hi, lo + 1))))

            # normalized, downsampled, truly isotropic volume for rendering:
            # every voxel is `self.voxel` µm in x, y and z, so the scale bar is valid for any rotation
            f = int(np.ceil(max(Y, X) / 256))
            while Z > 1 and Z * m.dz / (m.px * f) > 400:      # keep the resampled depth manageable
                f += 1
            ds = vol[:, :, : (Y // f) * f, : (X // f) * f].astype(np.float32)
            ds = ds.reshape(Z, C, Y // f, f, X // f, f).mean((3, 5))
            d["voxel"] = m.px * f                             # µm per rendered pixel
            zf = m.dz / d["voxel"] if Z > 1 else 1.0
            yf = m.py / m.px                                  # ≈1; corrects non-square pixels
            if abs(zf - 1) > 1e-3 or abs(yf - 1) > 1e-3:
                ds = ndimage.zoom(ds, (zf, 1, yf, 1), order=1)
            for c in range(C):
                lo, hi = d["lims"][c]
                ds[:, c] = np.clip((ds[:, c] - lo) / (hi - lo), 0, 1)
            d["iso"] = ds
            d["zscale"] = zf
            d["bar_um"] = nice_bar(X * m.px)                  # e.g. 100 µm for an 800 µm field
            if compute_only:
                out.update(d)
            else:
                self._prepare(vol, prepared=d)

        # ---- rotating MIP ---------------------------------------------------
        def _build_mip_tab(self, nb):
            fr = ttk.Frame(nb)
            nb.add(fr, text="Rotating projection")
            ctl = ttk.Frame(fr)
            ctl.pack(side="top", fill="x", padx=6, pady=4)
            self.az = tk.DoubleVar(value=30)
            self.el = tk.DoubleVar(value=20)
            self.gamma3 = tk.DoubleVar(value=1.0)
            for lbl, var, a, b in (("Azimuth", self.az, -180, 180), ("Elevation", self.el, -90, 90),
                                   ("Gamma", self.gamma3, 0.3, 2.0)):
                ttk.Label(ctl, text=lbl).pack(side="left")
                s = tk.Scale(ctl, variable=var, from_=a, to=b, orient="horizontal", length=180,
                             resolution=1 if lbl != "Gamma" else 0.05, showvalue=True)
                s.pack(side="left", padx=4)
                s.bind("<ButtonRelease-1>", lambda e: self.render_mip())
            ttk.Button(ctl, text="Top (XY)", command=lambda: self._set_view(0, 0)).pack(side="left", padx=2)
            ttk.Button(ctl, text="Side (XZ)", command=lambda: self._set_view(0, 90)).pack(side="left", padx=2)
            ttk.Button(ctl, text="Side (YZ)", command=lambda: self._set_view(90, 90)).pack(side="left", padx=2)
            ctl2 = ttk.Frame(fr)
            ctl2.pack(side="top", fill="x", padx=6, pady=(0, 4))
            ttk.Button(ctl2, text="Save rotation as TIFF…", command=self.save_rotation).pack(side="left")
            self.mip_status = ttk.Label(ctl2, text="")
            self.mip_status.pack(side="left", padx=8)
            self.fig_mip = Figure(figsize=(7, 6), facecolor="black")
            self.ax_mip = self.fig_mip.add_axes([0, 0, 1, 1])
            self.ax_mip.set_axis_off()
            self.canvas_mip = FigureCanvasTkAgg(self.fig_mip, master=fr)
            self.canvas_mip.get_tk_widget().pack(fill="both", expand=True)

        def _set_view(self, az, el):
            self.az.set(az)
            self.el.set(el)
            self.render_mip()

        def _render(self, az, el, gamma):
            v = self.iso  # (Z,C,Y,X)
            out = []
            for c in range(v.shape[1]):
                a = v[:, c]
                if el:
                    a = ndimage.rotate(a, el, axes=(0, 1), order=1, reshape=True)
                if az:
                    a = ndimage.rotate(a, az, axes=(1, 2), order=1, reshape=True)
                out.append(a.max(0))
            H = max(o.shape[0] for o in out)
            W = max(o.shape[1] for o in out)
            rgb = np.zeros((H, W, 3), np.float32)
            for c, o in enumerate(out):
                pad = np.zeros((H, W), np.float32)
                pad[:o.shape[0], :o.shape[1]] = o
                rgb += (np.clip(pad, 0, 1) ** gamma)[..., None] * np.asarray(self.colors[c], np.float32)
            return np.clip(rgb, 0, 1)

        def render_mip(self):
            if self._busy:
                return
            self._busy = True
            self.mip_status.config(text="rendering…")
            az, el, g = self.az.get(), self.el.get(), self.gamma3.get()
            res = {}

            def work():
                try:
                    res["rgb"] = self._render(az, el, g)
                except Exception as ex:  # noqa
                    res["err"] = ex

            th = threading.Thread(target=work, daemon=True)
            th.start()

            def poll():
                if th.is_alive():
                    self.after(50, poll)
                    return
                self._busy = False
                if "err" in res:
                    self.mip_status.config(text=f"error: {res['err']}")
                    return
                self.ax_mip.clear()
                self.ax_mip.set_axis_off()
                self.ax_mip.imshow(res["rgb"], interpolation="bilinear")
                if self.m.is_spatial:
                    self.ax_mip.add_artist(AnchoredSizeBar(
                        self.ax_mip.transData, self.bar_um / self.voxel, f"{self.bar_um:g} µm", "lower right",
                        pad=0.6, color="white", frameon=False, size_vertical=max(res["rgb"].shape[0] / 200, 1),
                        fontproperties={"size": 10}))
                self.canvas_mip.draw_idle()
                self.mip_status.config(text=f"az {az:.0f}°, el {el:.0f}°   (scale bar: lengths in the screen plane)")

            poll()

        def save_rotation(self):
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".tif",
                                                initialfile=default_stem(self.m) + "_rotation.tif",
                                                filetypes=[("TIFF", "*.tif")])
            if not path:
                return
            el, g = self.el.get(), self.gamma3.get()
            self.mip_status.config(text="rendering 36 frames…")
            self.update_idletasks()
            frames = [self._render(a, el, g) for a in range(-180, 180, 10)]
            H = max(f.shape[0] for f in frames)
            W = max(f.shape[1] for f in frames)
            stack = np.zeros((len(frames), H, W, 3), np.uint8)
            for i, f in enumerate(frames):
                stack[i, :f.shape[0], :f.shape[1]] = (f * 255).astype(np.uint8)
            if self.m.is_spatial:   # burn a white scale bar (bar_um long) into the lower-right corner
                L = int(round(self.bar_um / self.voxel))
                t = max(2, H // 150)
                x1, y1 = W - max(8, W // 30), H - max(8, H // 30)
                stack[:, y1 - t:y1, max(0, x1 - L):x1] = 255
            import tifffile
            # calibrated in µm, so Fiji's Analyze > Tools > Scale Bar also works
            tifffile.imwrite(path, stack, imagej=True, resolution=(1 / self.voxel, 1 / self.voxel),
                             metadata={"axes": "TYXS", "unit": "micron",
                                       "Info": f"rotating MIP, scale bar = {self.bar_um:g} um, "
                                               f"{self.voxel:.4g} um/pixel (isotropic)"})
            self.mip_status.config(text=f"saved {os.path.basename(path)}")

        # ---- orthogonal views -----------------------------------------------
        def _build_ortho_tab(self, nb):
            fr = ttk.Frame(nb)
            nb.add(fr, text="Orthogonal views")
            Z, C, Y, X = self.vol.shape
            self.oz = tk.IntVar(value=Z // 2)
            self.ox, self.oy = X // 2, Y // 2
            ctl = ttk.Frame(fr)
            ctl.pack(side="top", fill="x", padx=6, pady=4)
            ttk.Label(ctl, text="z plane").pack(side="left")
            tk.Scale(ctl, variable=self.oz, from_=0, to=max(Z - 1, 0), orient="horizontal", length=300,
                     command=lambda _v: self.render_ortho()).pack(side="left")
            ttk.Label(ctl, text="click in XY to move the XZ / YZ cut lines").pack(side="left", padx=10)
            self.fig_o = Figure(figsize=(7, 6))
            zr = max(Z * self.m.dz / self.m.py, 1)
            gs = self.fig_o.add_gridspec(2, 2, width_ratios=[X * self.m.px, zr * self.m.py],
                                         height_ratios=[Y * self.m.py, zr * self.m.py], wspace=0.05, hspace=0.05)
            self.ax_xy = self.fig_o.add_subplot(gs[0, 0])
            self.ax_yz = self.fig_o.add_subplot(gs[0, 1])
            self.ax_xz = self.fig_o.add_subplot(gs[1, 0])
            self.canvas_o = FigureCanvasTkAgg(self.fig_o, master=fr)
            self.canvas_o.get_tk_widget().pack(fill="both", expand=True)
            self.canvas_o.mpl_connect("button_press_event", self._ortho_click)
            self.render_ortho()

        def _ortho_click(self, ev):
            if ev.inaxes is self.ax_xy and ev.xdata is not None:
                self.ox, self.oy = int(round(ev.xdata / self.m.px)), int(round(ev.ydata / self.m.py))
                self.render_ortho()

        def render_ortho(self):
            m, v = self.m, self.vol
            Z, C, Y, X = v.shape
            z = int(self.oz.get())
            ox = int(np.clip(self.ox, 0, X - 1))
            oy = int(np.clip(self.oy, 0, Y - 1))
            vis = [True] * C
            L = self.plane_lims
            xy = to_rgb(v[z], self.colors, vis, L)
            xz = to_rgb(v[:, :, oy, :].transpose(1, 0, 2), self.colors, vis, L)   # (Z,X)
            yz = to_rgb(v[:, :, :, ox].transpose(1, 2, 0), self.colors, vis, L)   # (Y,Z)
            Wd, Hd, Dd = X * m.px, Y * m.py, Z * m.dz
            for ax in (self.ax_xy, self.ax_xz, self.ax_yz):
                ax.clear()
            self.ax_xy.imshow(xy, extent=(0, Wd, Hd, 0), aspect="auto")
            self.ax_xy.axvline(ox * m.px, color="y", lw=0.6)
            self.ax_xy.axhline(oy * m.py, color="y", lw=0.6)
            self.ax_xy.set_title(f"XY  z={z}  ({m.zlevels[z]:.1f} {m.d3unit})", fontsize=9)
            self.ax_xz.imshow(xz, extent=(0, Wd, Dd, 0), aspect="auto")
            self.ax_xz.axhline(z * m.dz, color="y", lw=0.6)
            self.ax_xz.set_xlabel(f"x ({m.xunit})")
            self.ax_xz.set_ylabel(f"z ({m.d3unit})")
            self.ax_yz.imshow(yz, extent=(0, Dd, Hd, 0), aspect="auto")
            self.ax_yz.axvline(z * m.dz, color="y", lw=0.6)
            self.ax_yz.set_xlabel(f"z ({m.d3unit})")
            self.ax_yz.set_yticklabels([])
            self.ax_xy.set_xticklabels([])
            self.ax_xy.set_ylabel(f"y ({m.yunit})")
            self.canvas_o.draw_idle()

    # -------------------------------------------------------------------------
    class RoiPlot(tk.Toplevel):
        """Graph of the polygon ROIs: profile across planes (raw / bg-subtracted / ΔF/F),
        or a bar chart of the current image (plane or projection)."""

        F0_METHODS = {"mean of first N points": "first", "percentile of trace": "percentile",
                      "minimum of trace": "min"}

        def __init__(self, app):
            super().__init__(app)
            self.app = app
            self.title("ROI graph")
            self.geometry("1000x650")
            self.protocol("WM_DELETE_WINDOW", self._close)
            self.data = None

            ctl = ttk.Frame(self, padding=4)
            ctl.pack(side="top", fill="x")
            r1 = ttk.Frame(ctl)
            r1.pack(fill="x")
            self.kind = tk.StringVar(value="profile")
            self.rb_prof = ttk.Radiobutton(r1, text="Profile across planes", variable=self.kind,
                                           value="profile", command=self.refresh)
            self.rb_prof.pack(side="left")
            ttk.Radiobutton(r1, text="Current image (bars)", variable=self.kind, value="bars",
                            command=self.refresh).pack(side="left", padx=(6, 14))
            ttk.Label(r1, text="Channel").pack(side="left")
            self.chan = tk.StringVar()
            self.chan_cb = ttk.Combobox(r1, textvariable=self.chan, state="readonly", width=16)
            self.chan_cb.pack(side="left", padx=3)
            self.chan_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            ttk.Label(r1, text="Background ROI").pack(side="left", padx=(10, 0))
            self.bgroi = tk.StringVar(value="none")
            self.bg_cb = ttk.Combobox(r1, textvariable=self.bgroi, state="readonly", width=8)
            self.bg_cb.pack(side="left", padx=3)
            self.bg_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            ttk.Label(r1, text="x axis").pack(side="left", padx=(10, 0))
            self.xaxis = tk.StringVar(value="z position")
            xa = ttk.Combobox(r1, textvariable=self.xaxis, state="readonly", width=11,
                              values=["z position", "plane index"])
            xa.pack(side="left", padx=3)
            xa.bind("<<ComboboxSelected>>", lambda e: self.refresh())

            r2 = ttk.Frame(ctl)
            r2.pack(fill="x", pady=(3, 0))
            self.dff = tk.BooleanVar(value=False)
            self.dff_cb = ttk.Checkbutton(r2, text="ΔF/F", variable=self.dff, command=self.refresh)
            self.dff_cb.pack(side="left")
            ttk.Label(r2, text="F0 =").pack(side="left", padx=(6, 0))
            self.f0 = tk.StringVar(value="mean of first N points")
            f0cb = ttk.Combobox(r2, textvariable=self.f0, state="readonly", width=22,
                                values=list(self.F0_METHODS))
            f0cb.pack(side="left", padx=3)
            f0cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            ttk.Label(r2, text="N").pack(side="left")
            self.f0n = tk.StringVar(value="3")
            e1 = ttk.Entry(r2, textvariable=self.f0n, width=4)
            e1.pack(side="left", padx=2)
            ttk.Label(r2, text="percentile").pack(side="left", padx=(6, 0))
            self.f0p = tk.StringVar(value="10")
            e2 = ttk.Entry(r2, textvariable=self.f0p, width=4)
            e2.pack(side="left", padx=2)
            for e in (e1, e2):
                e.bind("<Return>", lambda ev: self.refresh())
            self.all_ch = tk.BooleanVar(value=False)
            ttk.Checkbutton(r2, text="all channels in bars", variable=self.all_ch,
                            command=self.refresh).pack(side="left", padx=10)
            ttk.Button(r2, text="Refresh", command=self.refresh).pack(side="left", padx=4)
            ttk.Button(r2, text="Save PNG…", command=self.save_png).pack(side="left", padx=2)
            ttk.Button(r2, text="Save data CSV…", command=self.save_csv).pack(side="left", padx=2)
            self.note = ttk.Label(ctl, text="", foreground="#a05000", wraplength=950)
            self.note.pack(fill="x", pady=(3, 0))

            self.fig = Figure(figsize=(8, 5))
            self.ax = self.fig.add_subplot(111)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            tb = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
            tb.update()
            tb.pack(side="bottom", fill="x")
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.sync()

        def _close(self):
            self.app.plot_win = None
            self.destroy()

        def sync(self):
            """Refresh choice lists from the app, then redraw."""
            app = self.app
            names = app.ch_names()
            self.chan_cb.config(values=names)
            if self.chan.get() not in names and names:
                self.chan.set(names[-1] if app.derived is not None else names[0])
            rois = [r["name"] for r in app._roi_list()] if app.m else []
            self.bg_cb.config(values=["none"] + rois)
            if self.bgroi.get() not in rois:
                self.bgroi.set("none")
            stack = app.vol is not None and app.vol.shape[0] > 1
            self.rb_prof.config(state="normal" if stack else "disabled")
            if not stack:
                self.kind.set("bars")
            self.refresh()

        # -- data --------------------------------------------------------------
        def _chan_volume(self, ci):
            app = self.app
            if app.nr_active() and app.nr_measure.get():
                return app.fvol[:, ci]
            n = len(app.m.channels)
            return app.vol[:, ci] if ci < n else app.derived

        def refresh(self):
            app = self.app
            self.ax.clear()
            self.note.config(text="")
            self.data = None
            rois = app._roi_list() if app.m else []
            if app.vol is None or not rois:
                self.ax.text(0.5, 0.5, "Draw polygon ROIs in the main window", ha="center", va="center",
                             transform=self.ax.transAxes, color="#777")
                self.canvas.draw_idle()
                return
            names = app.ch_names()
            ci = names.index(self.chan.get()) if self.chan.get() in names else 0
            shape = app.vol.shape[2:]
            masks = [polygon_mask(r["verts"], shape) for r in rois]
            bgname = self.bgroi.get()
            try:
                if self.kind.get() == "profile" and app.vol.shape[0] > 1:
                    self._profile(rois, masks, ci, names[ci], bgname)
                else:
                    self._bars(rois, masks, ci, names, bgname)
            except Exception as ex:
                traceback.print_exc()
                self.note.config(text=f"error: {ex}")
            self.fig.tight_layout()
            self.canvas.draw_idle()

        def _profile(self, rois, masks, ci, cname, bgname):
            app, m = self.app, self.app.m
            tr = roi_traces(self._chan_volume(ci), masks)          # (n_roi, Z)
            labels = [r["name"] for r in rois]
            ylabel = f"{cname} mean F (a.u.)"
            if bgname != "none":
                bi = labels.index(bgname)
                bg = tr[bi]
                keep = [i for i in range(len(labels)) if i != bi]
                tr = tr[keep] - bg
                labels = [labels[i] for i in keep]
                ylabel = f"{cname} F − F({bgname})"
            Z = tr.shape[1] if tr.size else app.vol.shape[0]
            use_z = self.xaxis.get() == "z position" and np.isfinite(m.zlevels).all()
            x = m.zlevels if use_z else np.arange(Z)
            xlabel = f"z ({m.d3unit})" if use_z else "plane index"
            out = {"plane": np.arange(Z), "z": m.zlevels}
            notes = []
            if self.dff.get():
                meth = self.F0_METHODS[self.f0.get()]
                try:
                    n = int(self.f0n.get())
                    pct = float(self.f0p.get())
                except ValueError:
                    n, pct = 3, 10.0
                dff = []
                for lab, t in zip(labels, tr):
                    d, f0 = delta_f_over_f(t, meth, n, pct)
                    out[f"{lab}_F"] = t
                    out[f"{lab}_F0"] = np.full(Z, f0)
                    if not np.isfinite(f0) or f0 <= 0:
                        notes.append(f"{lab}: F0 = {f0:.3g} ≤ 0, ΔF/F undefined")
                    dff.append(d)
                tr = np.array(dff) if dff else tr
                ylabel = f"ΔF/F  ({cname})"
                tag = "_dFF"
            else:
                tag = "_F" if bgname == "none" else "_F-bg"
            for lab, t in zip(labels, tr):
                self.ax.plot(x, t, marker="o", ms=3, lw=1.2, label=lab)
                out[lab + tag] = t
            if self.dff.get():
                self.ax.axhline(0, color="#999", lw=0.8)
                if tr.size == 0 or not np.isfinite(tr).any():
                    self.ax.text(0.5, 0.5, "ΔF/F undefined: F0 ≤ 0 for every ROI.\n"
                                 "This happens when F is near zero after background-ROI or offset subtraction.\n"
                                 "Use the raw channel, no background ROI, or untick 'remove offsets'.",
                                 ha="center", va="center", transform=self.ax.transAxes, color="#a05000")
            self.ax.set_xlabel(xlabel)
            self.ax.set_ylabel(ylabel)
            self.ax.set_title(f"{m.label} — ROI profile across {m.axis3}", fontsize=10)
            if labels:
                self.ax.legend(fontsize=8, ncol=max(1, len(labels) // 8 + 1))
            self.ax.grid(alpha=0.3)
            if m.axis3.lower() == "z" or "stack" in m.context.lower():
                notes.insert(0, "x axis is depth (z-stack), not time: this is an intensity-vs-depth profile; "
                                "ΔF/F here is relative to the F0 planes, not a calcium transient.")
            self.note.config(text="  ".join(notes))
            self.data = out

        def _bars(self, rois, masks, ci, names, bgname):
            app, m = self.app, self.app.m
            img = app.measure_image()
            chans = list(range(img.shape[0])) if self.all_ch.get() else [ci]
            labels = [r["name"] for r in rois]
            bi = labels.index(bgname) if bgname != "none" else None
            keep = [i for i in range(len(labels)) if i != bi]
            width = 0.8 / len(chans)
            cols = app.ch_colors()
            out = {"ROI": [labels[i] for i in keep]}
            for j, c in enumerate(chans):
                means = np.array([roi_stats(img[c], masks[i])["mean"] for i in keep])
                sds = np.array([roi_stats(img[c], masks[i])["sd"] for i in keep])
                if bi is not None:
                    means = means - roi_stats(img[c], masks[bi])["mean"]
                col = tuple(0.75 * v for v in cols[c]) if min(cols[c]) < 0.9 else (0.4, 0.4, 0.4)
                derived = c >= len(m.channels)
                if derived:
                    col = tuple(0.5 + 0.5 * v for v in col)
                xs = np.arange(len(keep)) + (j - (len(chans) - 1) / 2) * width
                self.ax.bar(xs, means, width, yerr=sds, capsize=3, color=col, label=names[c],
                            edgecolor="k", lw=0.5, hatch="///" if derived else None)
                out[f"{names[c]}_mean"] = means
                out[f"{names[c]}_SD"] = sds
            self.ax.set_xticks(np.arange(len(keep)))
            self.ax.set_xticklabels(out["ROI"])
            self.ax.set_ylabel("mean F (a.u.)" + (f" − F({bgname})" if bi is not None else "") + "  ± SD (pixels)")
            self.ax.set_title(f"{m.label} — {app.img_desc}", fontsize=10)
            self.ax.axhline(0, color="#999", lw=0.8)
            if len(chans) > 1:
                self.ax.legend(fontsize=8)
            self.ax.grid(alpha=0.3, axis="y")
            if self.dff.get():
                self.note.config(text="ΔF/F needs a series (a profile across planes or frames); "
                                      "it is not applied to bar charts.")
            self.data = out

        # -- export -------------------------------------------------------------
        def save_png(self):
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".png",
                                             initialfile=default_stem(self.app.m) + "_roi_graph.png",
                                             filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
            if p:
                self.fig.savefig(p, dpi=200)

        def save_csv(self):
            if not self.data:
                return
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                             initialfile=default_stem(self.app.m) + "_roi_graph.csv",
                                             filetypes=[("CSV", "*.csv")])
            if not p:
                return
            keys = list(self.data)
            cols = [np.atleast_1d(self.data[k]) for k in keys]
            n = max(len(c) for c in cols)
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(keys)
                for i in range(n):
                    w.writerow([(c[i] if i < len(c) else "") for c in cols])

    # -------------------------------------------------------------------------
    class App(tk.Tk):
        CACHE_BYTES = 3 * 1024 ** 3

        def __init__(self, files=()):
            super().__init__()
            self.title(f"MES Viewer {__version__}")
            self._set_icon()
            if sys.platform == "darwin":       # files dropped on the Dock icon / opened from Finder
                try:
                    self.createcommand("::tk::mac::OpenDocument", lambda *p: self.open_paths(list(p)))
                except Exception:
                    pass
            self.geometry("1500x950")
            self.files = []
            self.rows = {}           # tree iid -> Measurement
            self.cache = OrderedDict()
            self.m = None            # current measurement
            self.vol = None          # (Z,C,Y,X)
            self.img = None          # current 2D (C,Y,X)
            self.img_desc = ""
            self.rois = {}           # measurement label -> list of ROI dicts
            self.results = []
            self.selector = None
            self._im_artist = None
            self._render_pending = False
            self._jobs = queue.Queue()
            self.derived = None      # (Z,Y,X) float32 A − k·B, or None
            self.fvol = None         # noise-reduced (Z,C',Y,X) float32, or None
            self.preview = None      # quick filtered version of what is on screen
            self._pv_req = None
            self._pv_after = None
            self._nr_req = None      # key of the filter job in flight / done
            self.derived_name = ""
            self.plot_win = None
            self._busy = 0
            self._build_ui()
            self.after(100, self._poll_jobs)
            if files:
                self.after(200, lambda: self.open_paths(files))

        def _set_icon(self):
            """Window / taskbar / Dock icon (all Toplevels inherit it)."""
            try:
                self._icon_img = tk.PhotoImage(file=resource_path("icons", "mes_viewer_256.png"))
                self.iconphoto(True, self._icon_img)
            except Exception:
                pass
            if sys.platform.startswith("win"):
                try:
                    self.iconbitmap(default=resource_path("icons", "mes_viewer.ico"))
                except Exception:
                    pass

        def show_about(self):
            if napari_available():
                d3 = "3D view: napari is installed and is used for the GPU volume view."
            elif FROZEN:
                d3 = ("3D view: built-in (rotating projection and orthogonal views).\n"
                      "napari is not included in the installer; see the README to add it\n"
                      "when running MES Viewer from source.")
            else:
                d3 = "3D view: built-in. For the GPU volume view: pip install \"napari[all]\""
            messagebox.showinfo(
                "About MES Viewer",
                f"MES Viewer {__version__}\nFemtonics .mes browser\n\n{d3}\n\n"
                "© Gergely Szarka, MIT License\nhttps://github.com/jerjely/MES-Viewer", parent=self)

        # ---- background jobs -----------------------------------------------
        def run_bg(self, work, done, msg="working…"):
            self._busy += 1
            self.progress.start(10)
            self.set_status(msg)

            def wrap():
                try:
                    r = work()
                    self._jobs.put((done, r, None))
                except Exception as ex:
                    self._jobs.put((done, None, (ex, traceback.format_exc())))

            threading.Thread(target=wrap, daemon=True).start()

        def _poll_jobs(self):
            try:
                while True:
                    done, r, err = self._jobs.get_nowait()
                    self._busy -= 1
                    if self._busy <= 0:
                        self._busy = 0
                        self.progress.stop()
                    if err:
                        print(err[1], file=sys.stderr)
                        messagebox.showerror("Error", str(err[0]), parent=self)
                        self.set_status(f"error: {err[0]}")
                    else:
                        done(r)
            except queue.Empty:
                pass
            self.after(80, self._poll_jobs)

        def set_status(self, s):
            self.status.config(text=s)

        # ---- layout -----------------------------------------------------------
        def _build_ui(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            self._build_menu()

            bar = ttk.Frame(self)
            bar.pack(side="top", fill="x", padx=4, pady=3)
            ttk.Button(bar, text="Open .mes…", command=self.open_dialog).pack(side="left")
            ttk.Button(bar, text="Open folder…", command=self.open_folder).pack(side="left", padx=3)
            ttk.Button(bar, text="Close all", command=self.close_all).pack(side="left", padx=3)
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
            ttk.Button(bar, text="Export stack → TIFF", command=self.export_stack).pack(side="left", padx=2)
            ttk.Button(bar, text="Export projection → TIFF", command=self.export_projection).pack(side="left", padx=2)
            ttk.Button(bar, text="Batch export all…", command=self.batch_export).pack(side="left", padx=2)
            ttk.Button(bar, text="3D view", command=self.open_3d).pack(side="left", padx=8)
            self.progress = ttk.Progressbar(bar, mode="indeterminate", length=120)
            self.progress.pack(side="right")

            self.status = ttk.Label(self, text="Open a .mes file to start", anchor="w", relief="sunken")
            self.status.pack(side="bottom", fill="x")

            vpaned = ttk.PanedWindow(self, orient="vertical")
            vpaned.pack(fill="both", expand=True)

            # overview table
            top = ttk.Frame(vpaned)
            self.cols = ["File", "Key", "Type", "Context", "Date", "Objective", "Channels", "Size (W×H)",
                         "Planes", "Step", "Depth", "Pixel", "FOV (µm)", "Avg", "Dwell", "Z first", "Comment"]
            widths = [110, 60, 45, 70, 170, 70, 70, 80, 50, 60, 55, 85, 85, 40, 60, 65, 120]
            self.tree = ttk.Treeview(top, columns=self.cols, show="headings", height=8, selectmode="browse")
            for c, w in zip(self.cols, widths):
                self.tree.heading(c, text=c, command=lambda c=c: self._sort_tree(c))
                self.tree.column(c, width=w, anchor="w", stretch=c in ("Date", "Comment"))
            ys = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
            xs = ttk.Scrollbar(top, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            ys.grid(row=0, column=1, sticky="ns")
            xs.grid(row=1, column=0, sticky="ew")
            top.rowconfigure(0, weight=1)
            top.columnconfigure(0, weight=1)
            self.tree.bind("<<TreeviewSelect>>", self._on_select)
            self.tree.tag_configure("stack", foreground="#0b5394")
            self.tree.tag_configure("single", foreground="#38761d")
            vpaned.add(top, weight=1)

            # bottom: controls | image | side notebook
            hp = ttk.PanedWindow(vpaned, orient="horizontal")
            vpaned.add(hp, weight=4)
            self._build_controls(hp)
            self._build_canvas(hp)
            self._build_side(hp)

        def _build_menu(self):
            mb = tk.Menu(self)
            fm = tk.Menu(mb, tearoff=0)
            fm.add_command(label="Open .mes file(s)…", command=self.open_dialog, accelerator="Ctrl+O")
            fm.add_command(label="Open folder…", command=self.open_folder)
            fm.add_command(label="Close all", command=self.close_all)
            fm.add_separator()
            fm.add_command(label="Export overview table (CSV)…", command=self.export_overview)
            fm.add_command(label="Export metadata of selected (JSON)…", command=self.export_metadata)
            fm.add_separator()
            fm.add_command(label="Quit", command=self.destroy)
            mb.add_cascade(label="File", menu=fm)
            em = tk.Menu(mb, tearoff=0)
            em.add_command(label="Stack → multi-page TIFF (all channels)…", command=self.export_stack)
            em.add_command(label="Stack → one TIFF per channel…", command=lambda: self.export_stack(per_channel=True))
            em.add_command(label="Noise-reduced stack → multi-page TIFF…",
                           command=lambda: self.export_stack(filtered=True))
            em.add_command(label="Current projection → TIFF…", command=self.export_projection)
            em.add_command(label="Current view → PNG…", command=self.export_png)
            em.add_separator()
            em.add_command(label="Batch export every measurement → TIFF…", command=self.batch_export)
            em.add_separator()
            em.add_command(label="ROI results → CSV…", command=self.export_results)
            em.add_command(label="ROIs → JSON…", command=self.export_rois)
            mb.add_cascade(label="Export", menu=em)
            vm = tk.Menu(mb, tearoff=0)
            vm.add_command(label="3D view (napari if installed)", command=self.open_3d)
            vm.add_command(label="3D view (built-in)", command=lambda: self.open_3d(force_builtin=True))
            vm.add_separator()
            vm.add_command(label="ROI graph (profile / ΔF/F / bars)…", command=lambda: self.open_plot())
            mb.add_cascade(label="View", menu=vm)
            hm = tk.Menu(mb, tearoff=0)
            hm.add_command(label="About MES Viewer", command=self.show_about)
            mb.add_cascade(label="Help", menu=hm)
            self.config(menu=mb)
            self.bind("<Control-o>", lambda e: self.open_dialog())

        def _build_controls(self, parent):
            outer = ttk.Frame(parent, width=300)
            parent.add(outer, weight=0)
            # scrollable container so all controls stay reachable on small screens
            cv = tk.Canvas(outer, highlightthickness=0, width=290)
            sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            cv.pack(side="left", fill="both", expand=True)
            f = ttk.Frame(cv, padding=6)
            win = cv.create_window((0, 0), window=f, anchor="nw")
            f.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
            cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))

            def _wheel(ev):
                if ev.num == 4 or getattr(ev, "delta", 0) > 0:
                    cv.yview_scroll(-2, "units")
                else:
                    cv.yview_scroll(2, "units")

            def _bind_wheel(_e):
                cv.bind_all("<MouseWheel>", _wheel)
                cv.bind_all("<Button-4>", _wheel)
                cv.bind_all("<Button-5>", _wheel)

            def _unbind_wheel(_e):
                cv.unbind_all("<MouseWheel>")
                cv.unbind_all("<Button-4>")
                cv.unbind_all("<Button-5>")

            outer.bind("<Enter>", _bind_wheel)
            outer.bind("<Leave>", _unbind_wheel)

            self.info_lbl = ttk.Label(f, text="—", wraplength=270, justify="left")
            self.info_lbl.pack(fill="x", pady=(0, 6))

            # channels
            self.ch_frame = ttk.LabelFrame(f, text="Channels", padding=4)
            self.ch_frame.pack(fill="x", pady=3)
            self.ch_vars = []

            # channel subtraction (e.g. remove red bleed-through / autofluorescence from green)
            sf = ttk.LabelFrame(f, text="Channel subtraction", padding=4)
            sf.pack(fill="x", pady=3)
            self.sub_on = tk.BooleanVar(value=False)
            self.sub_a = tk.StringVar(value="UG")
            self.sub_b = tk.StringVar(value="UR")
            self.sub_k = tk.StringVar(value="1.0")
            self.sub_bg = tk.BooleanVar(value=True)
            self.sub_bgpct = tk.StringVar(value="5")
            self.sub_clip = tk.BooleanVar(value=False)
            ttk.Checkbutton(sf, text="Show  A − k × B  as an extra channel", variable=self.sub_on,
                            command=self._update_derived).grid(row=0, column=0, columnspan=4, sticky="w")
            ttk.Label(sf, text="A").grid(row=1, column=0, sticky="w")
            self.sub_a_cb = ttk.Combobox(sf, textvariable=self.sub_a, state="readonly", width=6)
            self.sub_a_cb.grid(row=1, column=1, sticky="w")
            ttk.Label(sf, text="B").grid(row=1, column=2, sticky="e")
            self.sub_b_cb = ttk.Combobox(sf, textvariable=self.sub_b, state="readonly", width=6)
            self.sub_b_cb.grid(row=1, column=3, sticky="w")
            for cbx in (self.sub_a_cb, self.sub_b_cb):
                cbx.bind("<<ComboboxSelected>>", lambda e: self._update_derived())
            ttk.Label(sf, text="k").grid(row=2, column=0, sticky="w")
            ke = ttk.Entry(sf, textvariable=self.sub_k, width=8)
            ke.grid(row=2, column=1, sticky="w")
            ke.bind("<Return>", lambda e: self._update_derived())
            ke.bind("<FocusOut>", lambda e: self._update_derived())
            ttk.Button(sf, text="k from last ROI", command=self.k_from_roi).grid(row=2, column=2, columnspan=2,
                                                                                 sticky="ew")
            ttk.Checkbutton(sf, text="remove offsets first (percentile)", variable=self.sub_bg,
                            command=self._update_derived).grid(row=3, column=0, columnspan=3, sticky="w")
            be = ttk.Entry(sf, textvariable=self.sub_bgpct, width=5)
            be.grid(row=3, column=3, sticky="w")
            be.bind("<Return>", lambda e: self._update_derived())
            ttk.Checkbutton(sf, text="clip negative values to 0", variable=self.sub_clip,
                            command=self._update_derived).grid(row=4, column=0, columnspan=4, sticky="w")
            self.sub_info = ttk.Label(sf, text="", wraplength=260, foreground="#666")
            self.sub_info.grid(row=5, column=0, columnspan=4, sticky="w")
            sf.columnconfigure(3, weight=1)
            self.sub_frame = sf

            # display mode
            mf = ttk.LabelFrame(f, text="Display", padding=4)
            mf.pack(fill="x", pady=3)
            self.mode = tk.StringVar(value="plane")
            ttk.Radiobutton(mf, text="Single plane", variable=self.mode, value="plane",
                            command=self.request_render).grid(row=0, column=0, sticky="w")
            ttk.Radiobutton(mf, text="Projection", variable=self.mode, value="proj",
                            command=self.request_render).grid(row=0, column=1, sticky="w")
            self.plane = tk.IntVar(value=0)
            self.plane_lbl = ttk.Label(mf, text="plane")
            self.plane_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
            self.plane_scale = tk.Scale(mf, variable=self.plane, from_=0, to=0, orient="horizontal",
                                        showvalue=False, command=lambda _v: self._plane_changed())
            self.plane_scale.grid(row=2, column=0, columnspan=2, sticky="ew")
            mf.columnconfigure(1, weight=1)

            pf = ttk.LabelFrame(f, text="Z projection", padding=4)
            pf.pack(fill="x", pady=3)
            self.pmethod = tk.StringVar(value="max")
            ttk.Label(pf, text="Method").grid(row=0, column=0, sticky="w")
            cb = ttk.Combobox(pf, textvariable=self.pmethod, values=PROJ_METHODS, state="readonly", width=8)
            cb.grid(row=0, column=1, sticky="w")
            cb.bind("<<ComboboxSelected>>", lambda e: self._proj_changed())
            self.pz0 = tk.IntVar(value=0)
            self.pz1 = tk.IntVar(value=0)
            ttk.Label(pf, text="from").grid(row=1, column=0, sticky="w")
            self.pz0_scale = tk.Scale(pf, variable=self.pz0, from_=0, to=0, orient="horizontal",
                                      command=lambda _v: self._proj_changed())
            self.pz0_scale.grid(row=1, column=1, sticky="ew")
            ttk.Label(pf, text="to").grid(row=2, column=0, sticky="w")
            self.pz1_scale = tk.Scale(pf, variable=self.pz1, from_=0, to=0, orient="horizontal",
                                      command=lambda _v: self._proj_changed())
            self.pz1_scale.grid(row=2, column=1, sticky="ew")
            self.prange_lbl = ttk.Label(pf, text="")
            self.prange_lbl.grid(row=3, column=0, columnspan=2, sticky="w")
            pf.columnconfigure(1, weight=1)
            self.proj_frame = pf

            cf = ttk.LabelFrame(f, text="Contrast (percentiles, per channel)", padding=4)
            cf.pack(fill="x", pady=3)
            self.lo_pct = tk.DoubleVar(value=0.5)
            self.hi_pct = tk.DoubleVar(value=99.7)
            self.gamma = tk.DoubleVar(value=1.0)
            for r, (lbl, var, a, b, res) in enumerate((("low %", self.lo_pct, 0, 50, 0.1),
                                                       ("high %", self.hi_pct, 50, 100, 0.05),
                                                       ("gamma", self.gamma, 0.2, 3.0, 0.05))):
                ttk.Label(cf, text=lbl).grid(row=r, column=0, sticky="w")
                tk.Scale(cf, variable=var, from_=a, to=b, resolution=res, orient="horizontal",
                         command=lambda _v: self.request_render(recompute=False)).grid(row=r, column=1, sticky="ew")
            cf.columnconfigure(1, weight=1)

            nf = ttk.LabelFrame(f, text="Noise reduction", padding=4)
            nf.pack(fill="x", pady=3)
            self.nr_method = tk.StringVar(value="none")
            self.nr_sigma = tk.StringVar(value="1.0")
            self.nr_sigz = tk.StringVar(value="1.0")
            self.nr_size = tk.StringVar(value="3")
            self.nr_measure = tk.BooleanVar(value=False)
            self.nr_out = tk.BooleanVar(value=False)
            self.nr_out_r = tk.StringVar(value="2")
            self.nr_out_t = tk.StringVar(value="4")
            self.nr_out_w = tk.StringVar(value="bright")
            ttk.Checkbutton(nf, text="1. Remove outliers (hot pixels, small specks)", variable=self.nr_out,
                            command=self._nr_changed).grid(row=0, column=0, columnspan=4, sticky="w")
            for col, (lbl, var) in enumerate((("radius px", self.nr_out_r), ("> SD", self.nr_out_t))):
                ttk.Label(nf, text=lbl).grid(row=1, column=2 * col, sticky="w")
                e = ttk.Entry(nf, textvariable=var, width=5)
                e.grid(row=1, column=2 * col + 1, sticky="w")
                e.bind("<Return>", lambda ev: self._nr_changed())
            ttk.Label(nf, text="which").grid(row=2, column=0, sticky="w")
            ocb = ttk.Combobox(nf, textvariable=self.nr_out_w, values=OUTLIER_MODES, state="readonly", width=7)
            ocb.grid(row=2, column=1, columnspan=2, sticky="w")
            ocb.bind("<<ComboboxSelected>>", lambda e: self._nr_changed())
            ttk.Label(nf, text="2. Smoothing").grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))
            ncb = ttk.Combobox(nf, textvariable=self.nr_method, values=NR_METHODS, state="readonly", width=22)
            ncb.grid(row=4, column=0, columnspan=4, sticky="ew")
            ncb.bind("<<ComboboxSelected>>", lambda e: self._nr_changed())
            for col, (lbl, var) in enumerate((("σ xy px", self.nr_sigma), ("σ z planes", self.nr_sigz))):
                ttk.Label(nf, text=lbl).grid(row=5, column=2 * col, sticky="w")
                e = ttk.Entry(nf, textvariable=var, width=5)
                e.grid(row=5, column=2 * col + 1, sticky="w")
                e.bind("<Return>", lambda ev: self._nr_changed())
            ttk.Label(nf, text="median px").grid(row=6, column=0, sticky="w")
            e = ttk.Entry(nf, textvariable=self.nr_size, width=5)
            e.grid(row=6, column=1, sticky="w")
            e.bind("<Return>", lambda ev: self._nr_changed())
            ttk.Button(nf, text="Apply (stack)", command=self._update_nr).grid(row=6, column=2, columnspan=2,
                                                                               sticky="ew")
            self.nr_live = tk.BooleanVar(value=False)
            ttk.Button(nf, text="Preview (visible)", command=self.preview_nr).grid(row=7, column=0, columnspan=2,
                                                                                   sticky="ew", pady=(3, 0))
            ttk.Checkbutton(nf, text="live", variable=self.nr_live,
                            command=lambda: self.preview_nr() if self.nr_live.get() else self._clear_preview()
                            ).grid(row=7, column=2, columnspan=2, sticky="w", pady=(3, 0))
            ttk.Checkbutton(nf, text="use filtered data for ROI measurements / graphs",
                            variable=self.nr_measure, command=self._roi_changed).grid(row=9, column=0, columnspan=4,
                                                                                      sticky="w")
            self.nr_info = ttk.Label(nf, text="Display, projections and 3D view use the filtered stack.",
                                     wraplength=260, foreground="#666")
            self.nr_info.grid(row=8, column=0, columnspan=4, sticky="w")

            rf = ttk.LabelFrame(f, text="Polygon ROI measurement", padding=4)
            rf.pack(fill="x", pady=3)
            self.draw_var = tk.BooleanVar(value=False)
            ttk.Label(rf, text="Click vertices; click the first vertex to close.\n"
                               "Esc restarts the polygon being drawn.",
                      foreground="#666").grid(row=6, column=0, columnspan=2, sticky="w", pady=(3, 0))
            ttk.Checkbutton(rf, text="Draw polygons",
                            variable=self.draw_var, command=self._toggle_draw).grid(row=0, column=0, columnspan=2,
                                                                                    sticky="w")
            self.auto_measure = tk.BooleanVar(value=True)
            ttk.Checkbutton(rf, text="Measure automatically when closed",
                            variable=self.auto_measure).grid(row=1, column=0, columnspan=2, sticky="w")
            ttk.Button(rf, text="Measure all ROIs (current image)",
                       command=self.measure_all).grid(row=2, column=0, columnspan=2, sticky="ew", pady=1)
            ttk.Button(rf, text="Graph ROIs (profile / ΔF/F / bars)…",
                       command=self.open_plot).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 1))
            self.btn_across = ttk.Button(rf, text="Measure ROIs across all planes",
                                         command=self.measure_across)
            self.btn_across.grid(row=3, column=0, columnspan=2, sticky="ew", pady=1)
            ttk.Button(rf, text="Delete last ROI", command=self.delete_last_roi).grid(row=4, column=0, sticky="ew")
            ttk.Button(rf, text="Clear ROIs", command=self.clear_rois).grid(row=4, column=1, sticky="ew")
            ttk.Button(rf, text="Load ROIs (JSON)…", command=self.load_rois).grid(row=5, column=0, columnspan=2,
                                                                                  sticky="ew", pady=1)
            rf.columnconfigure(0, weight=1)
            rf.columnconfigure(1, weight=1)
            self.roi_hint = ttk.Label(f, text="", wraplength=270, foreground="#666")
            self.roi_hint.pack(fill="x")

        def _build_canvas(self, parent):
            fr = ttk.Frame(parent)
            parent.add(fr, weight=3)
            self.fig = Figure(figsize=(7, 7), facecolor="#202020")
            self.ax = self.fig.add_axes([0.01, 0.01, 0.98, 0.94])
            self.ax.set_axis_off()
            self.title_txt = self.fig.text(0.01, 0.975, "", color="w", fontsize=9, va="center")
            self.canvas = FigureCanvasTkAgg(self.fig, master=fr)
            tb = NavigationToolbar2Tk(self.canvas, fr, pack_toolbar=False)
            tb.update()
            tb.pack(side="bottom", fill="x")
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.canvas.mpl_connect("motion_notify_event", self._on_motion)
            self.canvas.mpl_connect("button_release_event", self._after_zoom)

        def _build_side(self, parent):
            nb = ttk.Notebook(parent, width=420)
            parent.add(nb, weight=1)
            self.side_nb = nb
            # metadata
            mf = ttk.Frame(nb)
            nb.add(mf, text="Metadata")
            ff = ttk.Frame(mf)
            ff.pack(fill="x", padx=3, pady=3)
            ttk.Label(ff, text="Filter").pack(side="left")
            self.meta_filter = tk.StringVar()
            ent = ttk.Entry(ff, textvariable=self.meta_filter)
            ent.pack(side="left", fill="x", expand=True, padx=3)
            ent.bind("<KeyRelease>", lambda e: self._fill_meta())
            ttk.Label(ff, text="frame").pack(side="left")
            self.meta_frame = tk.IntVar(value=0)
            self.meta_spin = ttk.Spinbox(ff, from_=0, to=0, width=5, textvariable=self.meta_frame,
                                         command=self._fill_meta)
            self.meta_spin.pack(side="left")
            self.meta = ttk.Treeview(mf, columns=("value",), show="tree headings")
            self.meta.heading("#0", text="Field")
            self.meta.heading("value", text="Value")
            self.meta.column("#0", width=200)
            self.meta.column("value", width=220)
            ys = ttk.Scrollbar(mf, orient="vertical", command=self.meta.yview)
            self.meta.configure(yscrollcommand=ys.set)
            ys.pack(side="right", fill="y")
            self.meta.pack(fill="both", expand=True)

            # results
            rf = ttk.Frame(nb)
            nb.add(rf, text="ROI results")
            self.rcols = ["ROI", "Measurement", "Source", "Channel", "Pixels", "Area", "Perimeter",
                          "Mean", "SD", "Min", "Max", "Median", "IntDen", "Cx", "Cy"]
            self.rtree = ttk.Treeview(rf, columns=self.rcols, show="headings")
            for c in self.rcols:
                self.rtree.heading(c, text=c)
                self.rtree.column(c, width=70 if c not in ("Measurement", "Source") else 120, anchor="w")
            ys = ttk.Scrollbar(rf, orient="vertical", command=self.rtree.yview)
            xs = ttk.Scrollbar(rf, orient="horizontal", command=self.rtree.xview)
            self.rtree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
            bb = ttk.Frame(rf)
            bb.pack(side="bottom", fill="x")
            ttk.Button(bb, text="Export CSV…", command=self.export_results).pack(side="left", padx=2, pady=2)
            ttk.Button(bb, text="Clear results", command=self.clear_results).pack(side="left", padx=2)
            xs.pack(side="bottom", fill="x")
            ys.pack(side="right", fill="y")
            self.rtree.pack(fill="both", expand=True)

        # ---- opening -----------------------------------------------------------
        def open_dialog(self):
            paths = filedialog.askopenfilenames(parent=self, title="Open MES file(s)",
                                                filetypes=[("Femtonics MES", "*.mes"), ("All files", "*.*")])
            if paths:
                self.open_paths(paths)

        def open_folder(self):
            d = filedialog.askdirectory(parent=self, title="Folder with .mes files")
            if d:
                paths = sorted(os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".mes"))
                if not paths:
                    messagebox.showinfo("No files", "No .mes files in that folder.", parent=self)
                    return
                self.open_paths(paths)

        def open_paths(self, paths):
            already = {f.path for f in self.files}
            paths = [p for p in paths if os.path.abspath(p) not in already]
            if not paths:
                return

            def work():
                out, errs = [], []
                for p in paths:
                    try:
                        out.append(MesFile(p))
                    except Exception as ex:
                        errs.append(f"{os.path.basename(p)}: {ex}")
                return out, errs

            def done(r):
                out, errs = r
                for mf in out:
                    self.files.append(mf)
                    for m in mf.measurements:
                        s = m.summary()
                        iid = self.tree.insert("", "end", values=[s[c] for c in self.cols],
                                               tags=("stack" if m.is_stack else "single",))
                        self.rows[iid] = m
                n = sum(len(f.measurements) for f in out)
                self.set_status(f"Opened {len(out)} file(s), {n} measurement(s)")
                if errs:
                    messagebox.showwarning("Some files failed", "\n".join(errs), parent=self)
                if out and not self.tree.selection():
                    first = self.tree.get_children()[0]
                    self.tree.selection_set(first)
                    self.tree.see(first)

            self.run_bg(work, done, f"reading headers of {len(paths)} file(s)…")

        def close_all(self):
            self.tree.delete(*self.tree.get_children())
            self.rows.clear()
            self.files.clear()
            self.cache.clear()
            self.m = self.vol = self.img = None
            self.ax.clear()
            self.ax.set_axis_off()
            self._im_artist = None
            self.title_txt.set_text("")
            self.canvas.draw_idle()
            self.meta.delete(*self.meta.get_children())
            self.info_lbl.config(text="—")

        def _sort_tree(self, col):
            items = [(self.tree.set(i, col), i) for i in self.tree.get_children("")]

            def key(t):
                try:
                    return (0, float(re.sub(r"[^\d.\-eE]", "", t[0].split("×")[0].split()[0])), "")
                except (ValueError, IndexError):
                    return (1, 0, t[0])

            rev = getattr(self, "_sort_rev", {}).get(col, False)
            items.sort(key=key, reverse=rev)
            for k, (_, i) in enumerate(items):
                self.tree.move(i, "", k)
            self._sort_rev = getattr(self, "_sort_rev", {})
            self._sort_rev[col] = not rev

        # ---- selection & loading ------------------------------------------------
        def _on_select(self, _e=None):
            sel = self.tree.selection()
            if not sel:
                return
            m = self.rows[sel[0]]
            if m is self.m:
                return
            self._show_measurement_info(m)
            if m.label in self.cache:
                self.cache.move_to_end(m.label)
                self._set_volume(m, self.cache[m.label])
                return
            self.run_bg(m.load, lambda vol, m=m: self._loaded(m, vol), f"loading {m.label}…")

        def _loaded(self, m, vol):
            self.cache[m.label] = vol
            while sum(v.nbytes for v in self.cache.values()) > self.CACHE_BYTES and len(self.cache) > 1:
                self.cache.popitem(last=False)
            sel = self.tree.selection()
            if sel and self.rows[sel[0]] is m:
                self._set_volume(m, vol)

        def _show_measurement_info(self, m):
            s = m.summary()
            txt = (f"{m.label}\n{m.type} / {m.context}   {m.objective}\n"
                   f"{m.width}×{m.height} px, {m.px:.3g} {m.xunit}/px\n")
            if m.is_stack:
                txt += f"{m.nplanes} planes × {m.dz:g} {m.d3unit} ({s['Depth']} {m.d3unit})\n"
            txt += f"channels: {', '.join(m.channels)}\n{m.date}"
            self.info_lbl.config(text=txt)
            self.meta_spin.config(to=len(m.frames) - 1)
            self.meta_frame.set(0)
            self._meta_m = m
            self._fill_meta()

        def _set_volume(self, m, vol):
            self._stop_selector()
            self.m, self.vol = m, vol
            Z = vol.shape[0]
            self.derived = None
            self.sub_a_cb.config(values=m.channels)
            self.sub_b_cb.config(values=m.channels)
            if len(m.channels) >= 2:
                if self.sub_a.get() not in m.channels:
                    self.sub_a.set(m.channels[0])
                if self.sub_b.get() not in m.channels or self.sub_b.get() == self.sub_a.get():
                    self.sub_b.set(next(c for c in m.channels if c != self.sub_a.get()))
            self._compute_derived()
            self.fvol = None
            self._nr_req = None
            self._rebuild_channel_checks()
            for sc in (self.plane_scale, self.pz0_scale, self.pz1_scale):
                sc.config(to=max(Z - 1, 0))
            self.plane.set(Z // 2 if Z > 1 else 0)
            self.pz0.set(0)
            self.pz1.set(Z - 1)
            state = "normal" if Z > 1 else "disabled"
            for sc in (self.plane_scale, self.pz0_scale, self.pz1_scale):
                sc.config(state=state)
            self.btn_across.config(state=state)
            self.prange_lbl.config(text="" if Z == 1 else f"{Z} planes")
            if Z == 1:
                self.mode.set("plane")
                self.roi_hint.config(text="Single image: draw polygons to measure.")
            else:
                self.roi_hint.config(text="Z-stack: measure on a plane, on a projection, "
                                          "or use 'across all planes' for a per-plane profile.")
            self._proj_cache = None
            self._im_artist = None
            self.ax.clear()
            self.ax.set_axis_off()
            self.request_render()
            self._roi_changed()
            self.set_status(f"{m.label}: volume {vol.shape} (Z,C,Y,X) {vol.dtype}")
            if getattr(self, "_nr_applied", False) and self._nr_wanted():
                self._update_nr()

        # ---- channels & subtraction ----------------------------------------------
        def ch_names(self):
            if self.m is None:
                return []
            return list(self.m.channels) + ([self.derived_name] if self.derived is not None else [])

        def ch_colors(self):
            if self.m is None:
                return []
            cols = [self.m.color(c) for c in range(len(self.m.channels))]
            if self.derived is not None:
                cols.append((0.0, 1.0, 0.0))   # corrected green
            return cols

        def _rebuild_channel_checks(self, visible=None):
            for w in self.ch_frame.winfo_children():
                w.destroy()
            names, cols = self.ch_names(), self.ch_colors()
            self.ch_vars = []
            for c, (ch, rgbc) in enumerate(zip(names, cols)):
                vis = True if visible is None else visible[c]
                v = tk.BooleanVar(value=vis)
                col = ("#000000" if min(rgbc) > 0.9 else
                       "#%02x%02x%02x" % tuple(int(255 * k * 0.75) for k in rgbc))
                cbt = tk.Checkbutton(self.ch_frame, text=ch, variable=v, fg=col, selectcolor="white",
                                     command=self.request_render)
                cbt.pack(side="left", padx=4)
                self.ch_vars.append(v)

        def _sub_params(self):
            m = self.m
            if m is None or len(m.channels) < 2:
                raise ValueError("needs a measurement with at least two channels")
            a, b = m.channels.index(self.sub_a.get()), m.channels.index(self.sub_b.get())
            if a == b:
                raise ValueError("A and B must be different channels")
            k = float(self.sub_k.get().replace(",", "."))
            offs = (0.0, 0.0)
            if self.sub_bg.get():
                pct = float(self.sub_bgpct.get().replace(",", "."))
                offs = tuple(channel_offsets(self.vol, [a, b], pct))
            return a, b, k, offs

        def _compute_derived(self):
            """(Re)build the A − k·B channel for the current volume. Returns True if it changed."""
            self.derived = None
            self.derived_name = ""
            if not self.sub_on.get() or self.vol is None:
                self.sub_info.config(text="")
                return
            try:
                a, b, k, offs = self._sub_params()
            except ValueError as ex:
                self.sub_info.config(text=str(ex))
                return
            self.derived = subtract_channels(self.vol, a, b, k, offs, self.sub_clip.get())
            self.derived_name = f"{self.m.channels[a]}−{k:.3g}·{self.m.channels[b]}"
            self.derived_info = dict(A=self.m.channels[a], B=self.m.channels[b], k=k,
                                     offset_A=offs[0], offset_B=offs[1], clip=self.sub_clip.get())
            self.sub_info.config(text=(f"offsets: {self.m.channels[a]} {offs[0]:.0f}, "
                                       f"{self.m.channels[b]} {offs[1]:.0f}" if self.sub_bg.get()
                                       else "no offset removal"))

        def _update_derived(self):
            if self.vol is None:
                return
            had = self.derived is not None
            self._compute_derived()
            has = self.derived is not None
            n = len(self.m.channels)
            if has and not had:      # first switch-on: show only the corrected channel
                vis = [False] * n + [True]
            elif has:
                vis = [v.get() for v in self.ch_vars[:n]] + [self.ch_vars[n].get() if len(self.ch_vars) > n else True]
            else:
                vis = [v.get() for v in self.ch_vars[:n]] if len(self.ch_vars) >= n else None
                if vis is not None and not any(vis):
                    vis = [True] * n
            self._rebuild_channel_checks(vis)
            self._proj_cache = None
            self.fvol = None
            self._nr_req = None
            self.request_render()
            self._roi_changed()
            if getattr(self, "_nr_applied", False) and self._nr_wanted():
                self._update_nr()

        def k_from_roi(self):
            if self.vol is None or not self._roi_list():
                messagebox.showinfo("k from ROI", "Draw a polygon around something that should disappear "
                                    "after subtraction (e.g. an autofluorescent spot visible in both "
                                    "channels), then press this button.", parent=self)
                return
            try:
                a, b, _k, offs = self._sub_params()
            except ValueError as ex:
                messagebox.showerror("k from ROI", str(ex), parent=self)
                return
            roi = self._roi_list()[-1]
            mask = polygon_mask(roi["verts"], self.vol.shape[2:])
            k = k_from_mask(self.vol, a, b, mask, offs)
            if not np.isfinite(k):
                messagebox.showerror("k from ROI", "Could not estimate k (no signal in channel B inside the ROI).",
                                     parent=self)
                return
            self.sub_k.set(f"{k:.4g}")
            self.sub_on.set(True)
            self._update_derived()
            self.set_status(f"k = {k:.4g} estimated from {roi['name']} (least squares over all planes)")

        def nr_active(self):
            return self.fvol is not None

        def _nr_wanted(self):
            return self.nr_out.get() or self.nr_method.get() != "none"

        def nr_params(self):
            f = lambda v: float(v.get().replace(",", "."))  # noqa: E731
            p = dict(method=self.nr_method.get(), sigma_xy=f(self.nr_sigma), sigma_z=f(self.nr_sigz),
                     size=int(f(self.nr_size)), outliers=self.nr_out.get(), out_radius=f(self.nr_out_r),
                     out_threshold=f(self.nr_out_t), out_which=self.nr_out_w.get())
            if p["sigma_xy"] < 0 or p["sigma_z"] < 0 or p["size"] < 1 or p["out_radius"] < 1 or p["out_threshold"] <= 0:
                raise ValueError("σ ≥ 0, median size ≥ 1, radius ≥ 1, threshold > 0")
            return p

        # ---- quick preview of the noise reduction (only what is visible) ---------------
        def _view_key(self):
            Z = self.vol.shape[0]
            if self.mode.get() == "proj" and Z > 1:
                v = ("proj", int(self.pz0.get()), int(self.pz1.get()), self.pmethod.get())
            else:
                v = ("plane", int(self.plane.get()) if Z > 1 else 0)
            return (self.m.label, id(self.vol), self.derived_name) + v

        def _params_key(self):
            try:
                return tuple(sorted(self.nr_params().items()))
            except ValueError:
                return None

        def _visible_crop(self):
            H, W = self.vol.shape[2:]
            x0, x1 = sorted(self.ax.get_xlim())
            y0, y1 = sorted(self.ax.get_ylim())
            c = (max(0, int(np.floor(y0))), min(H, int(np.ceil(y1)) + 1),
                 max(0, int(np.floor(x0))), min(W, int(np.ceil(x1)) + 1))
            if c[1] <= c[0] or c[3] <= c[2] or (c[0] == 0 and c[2] == 0 and c[1] == H and c[3] == W):
                return None
            return c

        def _clear_preview(self):
            self.preview = None
            self._pv_req = None
            self.request_render(recompute=False)

        def _schedule_preview(self):
            if self._pv_after is not None:
                self.after_cancel(self._pv_after)
            self._pv_after = self.after(250, self._run_scheduled_preview)

        def _run_scheduled_preview(self):
            self._pv_after = None
            self.preview_nr(auto=True)

        def preview_nr(self, auto=False):
            """Filter only the visible plane / projection (and only the zoomed region) for a quick look."""
            if self.vol is None:
                return
            if not self._nr_wanted():
                self._clear_preview()
                return
            try:
                p = self.nr_params()
            except ValueError as ex:
                self.nr_info.config(text=f"invalid value: {ex}")
                return
            pk = tuple(sorted(p.items()))
            # the full result for these settings is already displayed -> nothing to preview
            if self.nr_active() and tuple(sorted(getattr(self, "nr_used", {}).items())) == pk:
                self._clear_preview()
                return
            vk = self._view_key()
            crop = self._visible_crop()
            req = (vk, pk, crop)
            if self._pv_req == req:
                return
            self._pv_req = req
            base = self.display_volume(filtered=False)
            Z = base.shape[0]
            if vk[3] == "proj":
                kw = dict(proj=(vk[6], vk[4], vk[5]))
                approx = " (approx.: projection filtered after projecting)"
            else:
                kw = dict(z=vk[4])
                approx = ""
            desc = nr_description(p, self.m, Z)

            def work():
                return preview_filter(base, p, crop=crop, **kw)

            def done(img, req=req):
                if req != self._pv_req:
                    return
                self.preview = dict(view=req[0], params=req[1], img=img,
                                    desc=f"PREVIEW {desc}{approx}" + ("  — zoomed region only" if crop else ""))
                self.request_render(recompute=False)

            self.run_bg(work, done, "preview…")

        def _nr_changed(self):
            """A noise-reduction setting was edited: preview in live mode, otherwise wait for Apply."""
            if self.vol is None:
                return
            if not self._nr_wanted():
                self._update_nr()          # everything switched off -> back to raw at once
                return
            if self.nr_live.get():
                self.preview_nr()
            else:
                self.nr_info.config(text="settings changed — press Preview (visible) or Apply (stack)")

        def _update_nr(self):
            """(Re)compute the noise-reduced stack in the background."""
            if self.vol is None:
                return

            if not self._nr_wanted():
                self.fvol = None
                self._nr_req = None
                self._nr_applied = False
                self.preview = None
                self.nr_info.config(text="Display, projections and 3D view use the filtered stack.")
                self._proj_cache = None
                self.request_render()
                self._roi_changed()
                return
            try:
                p = self.nr_params()
            except ValueError as ex:
                self.nr_info.config(text=f"invalid value: {ex}")
                return
            base = self.display_volume(filtered=False)
            key = (self.m.label, id(self.vol), self.derived_name, tuple(sorted(p.items())))
            if key == self._nr_req and self.fvol is not None:
                return
            self._nr_req = key
            desc = nr_description(p, self.m, base.shape[0])
            self.nr_info.config(text=f"filtering… ({desc})")

            def done(res, key=key, desc=desc, p=p):
                if key != self._nr_req:          # settings or volume changed meanwhile
                    return
                self.fvol, info = res
                self.nr_desc = desc
                self.preview = None
                self._pv_req = None
                self._nr_applied = True
                self.nr_used = p
                self.nr_info.config(text=f"active: {desc}" + (f"  — {info}" if info else ""))
                self._proj_cache = None
                self.request_render()
                self._roi_changed()

            self.run_bg(lambda: apply_nr(base, p), done, f"noise reduction: {desc}…")

        def measure_image(self):
            """Image used for ROI numbers: raw unless 'use filtered data' is ticked."""
            if self.nr_active() and not self.nr_measure.get():
                return self._current_image(filtered=False, update_labels=False)[0]
            return self.img

        def plane_image(self, z, filtered=True):
            if filtered and self.nr_active():
                return self.fvol[z]
            img = self.vol[z].astype(np.float32)
            if self.derived is not None:
                img = np.concatenate([img, self.derived[z][None]], 0)
            return img

        def display_volume(self, filtered=False):
            """(Z,C',Y,X) incl. the derived channel (float32) if one is active;
            the noise-reduced version if `filtered` and noise reduction is on."""
            if filtered and self.nr_active():
                return self.fvol
            if self.derived is None:
                return self.vol
            return np.concatenate([self.vol.astype(np.float32), self.derived[:, None]], 1)

        # ---- metadata tree ------------------------------------------------------
        def _fill_meta(self):
            m = getattr(self, "_meta_m", None)
            self.meta.delete(*self.meta.get_children())
            if m is None:
                return
            try:
                i = int(self.meta_frame.get())
            except (tk.TclError, ValueError):
                i = 0
            i = int(np.clip(i, 0, len(m.frames) - 1))
            flt = self.meta_filter.get().strip().lower()

            def fmt(v):
                if isinstance(v, np.ndarray):
                    if v.size == 0:
                        return "[]"
                    if v.dtype == object:
                        return f"<{v.shape} cell>"
                    if v.size <= 12:
                        return np.array2string(v.ravel(), precision=6, separator=", ")
                    return f"<array {v.shape} {v.dtype}>"
                if isinstance(v, float):
                    return f"{v:.10g}"
                return str(v)

            def add(parent, name, v, path, depth):
                match = (not flt) or flt in path.lower()
                if _is_struct(v) and depth < 6:
                    node = self.meta.insert(parent, "end", text=name, values=("{…}",), open=bool(flt))
                    kept = False
                    for k in v._fieldnames:
                        kept |= add(node, k, getattr(v, k), f"{path}.{k}", depth + 1)
                    if not kept and not match:
                        self.meta.delete(node)
                        return False
                    return True
                if isinstance(v, np.ndarray) and v.dtype == object and v.size <= 50 and depth < 6:
                    node = self.meta.insert(parent, "end", text=name, values=(f"<{v.size} items>",))
                    kept = False
                    for j, it in enumerate(v.ravel()):
                        kept |= add(node, f"[{j}]", it, f"{path}[{j}]", depth + 1)
                    if not kept and not match:
                        self.meta.delete(node)
                        return False
                    return True
                if not match:
                    return False
                self.meta.insert(parent, "end", text=name, values=(fmt(v),))
                return True

            e = m.frames[i]
            for k in e._fieldnames:
                add("", k, getattr(e, k), k, 0)

        # ---- rendering ----------------------------------------------------------
        def _plane_changed(self):
            if self.vol is not None and self.vol.shape[0] > 1 and self.mode.get() != "plane":
                self.mode.set("plane")
            self.request_render()

        def _proj_changed(self):
            if self.vol is None:
                return
            if self.pz1.get() < self.pz0.get():
                self.pz1.set(self.pz0.get())
            if self.vol.shape[0] > 1:
                self.mode.set("proj")
            self._proj_cache = None
            self.request_render()

        def request_render(self, recompute=True):
            if recompute:
                self._need_recompute = True
            if not self._render_pending:
                self._render_pending = True
                self.after_idle(self._render)

        def _current_image(self, filtered=True, update_labels=True):
            m, vol = self.m, self.vol
            Z = vol.shape[0]
            filt = filtered and self.nr_active()
            tag = f"  [{self.nr_desc}]" if filt else ""
            if self.mode.get() == "proj" and Z > 1:
                z0, z1, meth = int(self.pz0.get()), int(self.pz1.get()), self.pmethod.get()
                key = (z0, z1, meth, filt)
                if getattr(self, "_proj_cache", None) and self._proj_cache[0] == key:
                    img = self._proj_cache[1]
                else:
                    if filt:     # filtering is done per plane / in 3D, then projected
                        img = project(self.fvol, meth, z0, z1)
                    else:
                        img = project(vol, meth, z0, z1)
                        if self.derived is not None:   # subtraction is done per plane, then projected
                            img = np.concatenate([img, project(self.derived[:, None], meth, z0, z1)], 0)
                    if filtered:
                        self._proj_cache = (key, img)
                if update_labels:
                    zt = (f"{m.zlevels[z0]:.1f}–{m.zlevels[z1]:.1f} {m.d3unit}"
                          if np.isfinite(m.zlevels[[z0, z1]]).all() else "")
                    self.prange_lbl.config(text=f"{z1 - z0 + 1} planes  {zt}")
                return img, f"{meth} projection, planes {z0}–{z1}{tag}", dict(kind="projection", method=meth,
                                                                               z0=z0, z1=z1, filtered=filt)
            z = int(self.plane.get()) if Z > 1 else 0
            zl = m.zlevels[z]
            if update_labels:
                self.plane_lbl.config(text=f"plane {z + 1}/{Z}" + (f"   z = {zl:.1f} {m.d3unit}"
                                                                    if np.isfinite(zl) else ""))
            desc = (f"plane {z}" if Z > 1 else "image") + tag
            return self.plane_image(z, filtered=filt), desc, dict(kind="plane", z=z, filtered=filt)

        def _render(self):
            self._render_pending = False
            if self.vol is None:
                return
            m = self.m
            if getattr(self, "_need_recompute", True) or self.img is None:
                self.img, self.img_desc, self.img_src = self._current_image()
                self._need_recompute = False
            disp, desc = self.img, self.img_desc
            pv = self.preview
            if pv is not None and pv["view"] == self._view_key() and pv["params"] == self._params_key() \
                    and pv["img"].shape == self.img.shape:
                disp = pv["img"]
                desc = self.img_desc.split("  [")[0] + f"  [{pv['desc']}]"
            elif self.nr_live.get() and self._nr_wanted():
                self._schedule_preview()
            vis = [v.get() for v in self.ch_vars] or [True] * disp.shape[0]
            n_vis = sum(vis)
            if len(vis) != disp.shape[0]:
                vis = (vis + [True] * disp.shape[0])[:disp.shape[0]]
            cc = self.ch_colors()
            colors = [cc[c] if n_vis > 1 else (1, 1, 1) for c in range(disp.shape[0])]
            lims = [auto_limits(disp[c], self.lo_pct.get(), self.hi_pct.get())
                    for c in range(disp.shape[0])]
            rgb = to_rgb(disp, colors, vis, lims, self.gamma.get())
            if self._im_artist is None or self._im_artist.get_array().shape[:2] != rgb.shape[:2]:
                self.ax.clear()
                self.ax.set_axis_off()
                self._im_artist = self.ax.imshow(rgb, interpolation="nearest")
                self._draw_scalebar()
                self._draw_rois()
            else:
                self._im_artist.set_data(rgb)
            self.title_txt.set_text(f"{m.label}   {m.context}   —   {desc}")
            self.canvas.draw_idle()

        def _draw_scalebar(self):
            m = self.m
            if not m.is_spatial:
                return
            L = nice_bar(m.width * m.px)
            bar = AnchoredSizeBar(self.ax.transData, L / m.px, f"{L:g} µm", "lower right",
                                  pad=0.4, color="white", frameon=False, size_vertical=max(m.height / 150, 1),
                                  fontproperties={"size": 9})
            self.ax.add_artist(bar)

        def _after_zoom(self, _ev):
            """In live-preview mode, refilter when zoom/pan changed the visible region."""
            if self.vol is not None and self.nr_live.get() and self._nr_wanted() and self.preview is not None:
                if self._pv_req and self._pv_req[2] != self._visible_crop():
                    self._schedule_preview()

        def _on_motion(self, ev):
            if ev.inaxes is not self.ax or self.img is None or ev.xdata is None:
                return
            m = self.m
            x, y = int(round(ev.xdata)), int(round(ev.ydata))
            if 0 <= x < self.img.shape[2] and 0 <= y < self.img.shape[1]:
                vals = "  ".join(f"{ch}={self.img[c, y, x]:.0f}" for c, ch in enumerate(self.ch_names())
                                 if c < self.img.shape[0])
                self.set_status(f"x={x} y={y} px   ({m.x0 + x * m.px:.1f}, {m.y0 + y * m.py:.1f} {m.xunit})"
                                f"   {vals}")

        # ---- ROIs -----------------------------------------------------------------
        def _roi_list(self):
            return self.rois.setdefault(self.m.label, []) if self.m else []

        def _toggle_draw(self):
            if self.draw_var.get():
                if self.m is None:
                    self.draw_var.set(False)
                    return
                self._start_selector()
                self.set_status("Draw polygon: click vertices, click the first vertex to close. "
                                "Esc to restart the current polygon.")
            else:
                self._stop_selector()

        def _start_selector(self):
            self._stop_selector(keep_flag=True)
            self.selector = PolygonSelector(self.ax, self._polygon_done, useblit=True,
                                            props=dict(color="yellow", linewidth=1.2))

        def _stop_selector(self, keep_flag=False):
            if self.selector is not None:
                self.selector.set_active(False)
                try:
                    self.selector.disconnect_events()
                    for a in getattr(self.selector, "artists", []):
                        a.set_visible(False)
                except Exception:
                    pass
                self.selector = None
                self.canvas.draw_idle()
            if not keep_flag:
                self.draw_var.set(False)

        def _polygon_done(self, verts):
            if len(verts) < 3:
                return
            rois = self._roi_list()
            roi = dict(name=f"ROI{len(rois) + 1}", verts=[(float(x), float(y)) for x, y in verts])
            rois.append(roi)
            self._draw_rois()
            self._roi_changed()
            if self.auto_measure.get():
                self._measure([roi])
            # restart the selector for the next polygon
            self.after(50, self._start_selector if self.draw_var.get() else (lambda: None))

        def _draw_rois(self):
            for p in getattr(self, "_roi_artists", []):
                try:
                    p.remove()
                except Exception:
                    pass
            self._roi_artists = []
            if self.m is None:
                return
            for r in self._roi_list():
                v = np.asarray(r["verts"])
                p = MplPolygon(v, closed=True, fill=False, edgecolor="yellow", lw=1.0)
                self.ax.add_patch(p)
                t = self.ax.text(v[:, 0].mean(), v[:, 1].mean(), r["name"], color="yellow",
                                 fontsize=8, ha="center", va="center")
                self._roi_artists += [p, t]
            self.canvas.draw_idle()

        def delete_last_roi(self):
            rois = self._roi_list()
            if rois:
                rois.pop()
                self._draw_rois()
                self._roi_changed()

        def clear_rois(self):
            if self.m:
                self.rois[self.m.label] = []
                self._draw_rois()
                self._roi_changed()

        def load_rois(self):
            if self.m is None:
                return
            p = filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json")])
            if not p:
                return
            with open(p) as fh:
                data = json.load(fh)
            lst = data.get(self.m.label) or (data if isinstance(data, list) else next(iter(data.values()), []))
            self.rois[self.m.label] = [dict(name=r["name"], verts=[tuple(v) for v in r["verts"]]) for r in lst]
            self._draw_rois()
            self._roi_changed()

        def _measure(self, rois, img=None, source=None, src_info=None):
            m = self.m
            if img is None:
                img = self.measure_image()
                if source is None and self.nr_active() and not self.nr_measure.get():
                    source = self.img_desc.split("  [")[0] + " (raw)"
            source = self.img_desc if source is None else source
            src_info = self.img_src if src_info is None else src_info
            area_unit = (f"{m.xunit}²" if m.xunit == m.yunit else f"{m.xunit}·{m.yunit}")
            new = []
            for r in rois:
                mask = polygon_mask(r["verts"], img.shape[1:])
                _, perim = roi_geometry(r["verts"], m.px, m.py)
                yy, xx = np.nonzero(mask)
                cx = (xx.mean() * m.px + m.x0) if xx.size else np.nan
                cy = (yy.mean() * m.py + m.y0) if yy.size else np.nan
                for c, ch in enumerate(self.ch_names()[:img.shape[0]]):
                    st = roi_stats(img[c], mask)
                    row = OrderedDict([
                        ("ROI", r["name"]), ("Measurement", m.label), ("Source", source), ("Channel", ch),
                        ("Pixels", st["n"]), ("Area", st["n"] * m.px * m.py), ("Perimeter", perim),
                        ("Mean", st["mean"]), ("SD", st["sd"]), ("Min", st["min"]), ("Max", st["max"]),
                        ("Median", st["median"]), ("IntDen", st["intden"]), ("Cx", cx), ("Cy", cy),
                        ("area_unit", area_unit), ("z_index", src_info.get("z", "")),
                        ("z_level", (float(m.zlevels[src_info["z"]]) if "z" in src_info
                                     and np.isfinite(m.zlevels[src_info["z"]]) else "")),
                        ("vertices_px", json.dumps(np.round(r["verts"], 2).tolist())),
                    ])
                    new.append(row)
            for row in new:
                self.results.append(row)
                self.rtree.insert("", "end", values=[self._fmt(row[c]) for c in self.rcols])
            if new:
                self.side_nb.select(1)
                self.rtree.see(self.rtree.get_children()[-1])
                self.set_status(f"measured {len(rois)} ROI(s) on {source}  (area in {area_unit})")

        @staticmethod
        def _fmt(v):
            if isinstance(v, float):
                return "nan" if not np.isfinite(v) else f"{v:.4g}"
            return v

        def measure_all(self):
            if self.m is None or not self._roi_list():
                messagebox.showinfo("No ROIs", "Draw at least one polygon first.", parent=self)
                return
            self._measure(self._roi_list())

        def measure_across(self):
            if self.vol is None or self.vol.shape[0] < 2:
                return
            rois = self._roi_list()
            if not rois:
                messagebox.showinfo("No ROIs", "Draw at least one polygon first.", parent=self)
                return
            for z in range(self.vol.shape[0]):
                filt = self.nr_measure.get() and self.nr_active()
                self._measure(rois, img=self.plane_image(z, filtered=filt),
                              source=f"plane {z}" + (" (filtered)" if filt else ""),
                              src_info=dict(kind="plane", z=z))

        def clear_results(self):
            self.results.clear()
            self.rtree.delete(*self.rtree.get_children())

        # ---- export -------------------------------------------------------------
        def _need_m(self):
            if self.m is None or self.vol is None:
                messagebox.showinfo("Nothing selected", "Select a loaded measurement first.", parent=self)
                return False
            return True

        def export_stack(self, per_channel=False, filtered=False):
            if not self._need_m():
                return
            if filtered and not self.nr_active():
                messagebox.showinfo("Noise reduction", "Choose a noise-reduction method first "
                                    "(and wait until it says 'active').", parent=self)
                return
            m, vol = self.m, self.display_volume(filtered=filtered)
            names, cols = self.ch_names(), self.ch_colors()
            extra = {"subtraction": self.derived_info} if self.derived is not None else {}
            if filtered:
                extra["noise_reduction"] = self.nr_desc
            extra = extra or None
            if per_channel:
                d = filedialog.askdirectory(parent=self, title="Folder for per-channel TIFFs")
                if not d:
                    return

                def work():
                    paths = []
                    for c, ch in enumerate(names):
                        p = os.path.join(d, f"{default_stem(m)}_{safe_name(ch)}.tif")
                        write_tiff(p, vol[:, c:c + 1], m, names=[ch], colors=[cols[c]], info_extra=extra)
                        paths.append(p)
                    return paths
                self.run_bg(work, lambda ps: self.set_status(f"wrote {len(ps)} files to {d}"), "writing TIFFs…")
                return
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".tif",
                                             initialfile=default_stem(m) + ("_denoised" if filtered else "") + ".tif",
                                             filetypes=[("TIFF (ImageJ hyperstack)", "*.tif")])
            if p:
                self.run_bg(lambda: write_tiff(p, vol, m, names=names, colors=cols, info_extra=extra),
                            lambda _r: self.set_status(f"wrote {p}"),
                            "writing TIFF…")

        def export_projection(self):
            if not self._need_m():
                return
            m = self.m
            if self.vol.shape[0] > 1 and self.mode.get() != "proj":
                self.mode.set("proj")
                self._proj_changed()
                self._render()
            img = self.img
            src = self.img_src
            suffix = (f"_{src['method']}_z{src['z0']}-{src['z1']}" if src.get("kind") == "projection"
                      else f"_plane{src.get('z', 0)}")
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".tif",
                                             initialfile=default_stem(m) + suffix + ".tif",
                                             filetypes=[("TIFF", "*.tif")])
            if not p:
                return
            arr = img[None]
            if (src.get("kind") == "projection" and src["method"] in ("max", "min") and self.vol.dtype == np.uint16
                    and self.derived is None):
                arr = arr.astype(np.uint16)
            extra = {"projection": src}
            if src.get("filtered"):
                extra["noise_reduction"] = self.nr_desc
            if self.derived is not None:
                extra["subtraction"] = self.derived_info
            names, cols = self.ch_names(), self.ch_colors()
            self.run_bg(lambda: write_tiff(p, arr, m, info_extra=extra, names=names, colors=cols),
                        lambda _r: self.set_status(f"wrote {p}"), "writing TIFF…")

        def export_png(self):
            if not self._need_m():
                return
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".png",
                                             initialfile=default_stem(self.m) + "_view.png",
                                             filetypes=[("PNG", "*.png")])
            if p:
                self.fig.savefig(p, dpi=200, facecolor=self.fig.get_facecolor())
                self.set_status(f"wrote {p}")

        def batch_export(self):
            if not self.files:
                return
            d = filedialog.askdirectory(parent=self, title="Output folder for batch export")
            if not d:
                return
            also_mip = messagebox.askyesno("Batch export", "Also write a max projection for every z-stack?",
                                           parent=self)
            ms = [m for f in self.files for m in f.measurements]

            def work():
                n = 0
                for m in ms:
                    vol = self.cache.get(m.label)
                    if vol is None:
                        vol = m.load()
                    write_tiff(os.path.join(d, default_stem(m) + ".tif"), vol, m)
                    n += 1
                    if also_mip and vol.shape[0] > 1:
                        mip = vol.max(0)[None]
                        write_tiff(os.path.join(d, default_stem(m) + "_MAX.tif"), mip, m,
                                   info_extra={"projection": {"method": "max", "z0": 0, "z1": vol.shape[0] - 1}})
                with open(os.path.join(d, "overview.csv"), "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(self.cols)
                    for m in ms:
                        s = m.summary()
                        w.writerow([s[c] for c in self.cols])
                return n

            self.run_bg(work, lambda n: messagebox.showinfo("Batch export", f"Wrote {n} stacks to\n{d}",
                                                            parent=self), f"batch exporting {len(ms)}…")

        def export_overview(self):
            if not self.rows:
                return
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile="mes_overview.csv",
                                             filetypes=[("CSV", "*.csv")])
            if not p:
                return
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(self.cols + ["Path"])
                for iid in self.tree.get_children():
                    m = self.rows[iid]
                    s = m.summary()
                    w.writerow([s[c] for c in self.cols] + [m.file.path])
            self.set_status(f"wrote {p}")

        def export_metadata(self):
            sel = self.tree.selection()
            if not sel:
                return
            m = self.rows[sel[0]]
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                             initialfile=default_stem(m) + "_metadata.json",
                                             filetypes=[("JSON", "*.json")])
            if not p:
                return
            data = {"summary": m.info_dict(),
                    "frames": [struct_to_jsonable(fr) for fr in m.frames]}
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, default=str, ensure_ascii=False)
            self.set_status(f"wrote {p}")

        def export_results(self):
            if not self.results:
                messagebox.showinfo("No results", "No ROI measurements yet.", parent=self)
                return
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile="roi_results.csv",
                                             filetypes=[("CSV", "*.csv")])
            if not p:
                return
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(self.results[0].keys()))
                w.writeheader()
                w.writerows(self.results)
            self.set_status(f"wrote {p}")

        def export_rois(self):
            data = {k: v for k, v in self.rois.items() if v}
            if not data:
                messagebox.showinfo("No ROIs", "No ROIs drawn.", parent=self)
                return
            p = filedialog.asksaveasfilename(parent=self, defaultextension=".json", initialfile="rois.json",
                                             filetypes=[("JSON", "*.json")])
            if p:
                with open(p, "w") as fh:
                    json.dump(data, fh, indent=1)
                self.set_status(f"wrote {p}")

        # ---- ROI graph -----------------------------------------------------------
        def open_plot(self):
            if not self._need_m():
                return
            if self.plot_win is not None and self.plot_win.winfo_exists():
                self.plot_win.lift()
                self.plot_win.sync()
                return
            self.plot_win = RoiPlot(self)

        def _roi_changed(self):
            if self.plot_win is not None:
                try:
                    if self.plot_win.winfo_exists():
                        self.plot_win.sync()
                except tk.TclError:
                    self.plot_win = None

        # ---- 3D -----------------------------------------------------------------
        def open_3d(self, force_builtin=False):
            if not self._need_m():
                return
            m, vol = self.m, self.display_volume(filtered=True)
            names, cols = self.ch_names(), self.ch_colors()
            if vol.shape[0] < 2:
                messagebox.showinfo("3D view", "This measurement is a single plane — nothing to show in 3D.",
                                    parent=self)
                return
            if not force_builtin:
                has_napari = napari_available()
                if has_napari:
                    tmpd = tempfile.mkdtemp(prefix="mesview_")
                    npy = os.path.join(tmpd, "vol.npy")
                    meta = os.path.join(tmpd, "meta.json")
                    np.save(npy, vol)
                    with open(meta, "w") as fh:
                        json.dump(dict(title=m.label, channels=names, colors=cols, dz=m.dz, px=m.px, py=m.py), fh)
                    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--napari", npy, meta])
                    self.set_status("launched napari in a separate window (may take a few seconds)")
                    return
                self.set_status("napari not available — using the built-in 3D view"
                                + ("" if FROZEN else " (pip install \"napari[all]\" for the GPU view)"))
            act = self.nr_active()
            v3 = Viewer3D(self, m, vol, colors=cols, raw=self.display_volume(filtered=False),
                          nr=(self.nr_used if act else None), nr_desc=(self.nr_desc if act else ""))
            if act:
                v3.title(f"3D view — {m.label}   [{self.nr_desc}]")

    return App


def main(argv):
    if len(argv) >= 4 and argv[1] == "--napari":
        run_napari(argv[2], argv[3])
        return 0
    if len(argv) >= 2 and argv[1] == "--version":
        print(f"MES Viewer {__version__}")
        return 0
    if len(argv) >= 2 and argv[1] == "--selftest":
        import mes_selftest
        return mes_selftest.run(argv[2] if len(argv) > 2 else "mes_selftest")
    if sys.platform.startswith("win"):
        try:    # own taskbar group + icon instead of python.exe's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    App = build_gui()
    app = App([a for a in argv[1:] if os.path.isfile(a)])
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
