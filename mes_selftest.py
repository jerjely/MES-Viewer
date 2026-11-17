"""
Self-test of MES Viewer, run by the release workflow on the *built* app before it is packaged:

    MES-Viewer --selftest <folder>          (installed / built app)
    python mes_viewer.py --selftest <folder>   (from source)

It writes two synthetic .mes files into <folder> (MATLAB v5 and v7.3/HDF5 layouts, see
mes_synthetic.py), then drives the real GUI through every main feature and compares the numbers
with the known contents. The result goes to <folder>/selftest.txt, first line "OK ..." or
"FAIL ...", and the process exit code is 0 (all passed) or 1 (anything failed), so a broken build
is never released.
"""

import os
import sys
import time
import traceback

import numpy as np


class SelfTestError(AssertionError):
    pass


def _check(cond, msg):
    if not cond:
        raise SelfTestError(msg)


def run(folder):
    folder = os.path.abspath(folder)
    os.makedirs(folder, exist_ok=True)
    report = os.path.join(folder, "selftest.txt")
    log = []
    t_start = time.time()

    def ok(msg):
        log.append(f"  ok  {msg}")
        print(f"  ok  {msg}", flush=True)

    try:
        _run(folder, ok)
        status = f"OK  MES Viewer self-test passed ({len(log)} checks, {time.time() - t_start:.1f} s)"
        code = 0
    except Exception as ex:   # noqa
        status = f"FAIL  {type(ex).__name__}: {ex}"
        log.append(traceback.format_exc())
        code = 1
    import mes_viewer
    text = "\n".join([status, f"version: {mes_viewer.__version__}", f"python: {sys.version.split()[0]}",
                      f"platform: {sys.platform}", f"frozen: {mes_viewer.FROZEN}", ""] + log) + "\n"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(status, flush=True)
    return code


def _run(folder, ok):
    import mes_synthetic as ms
    import mes_viewer as mv

    T = ms.TRUTH
    truth = ms.make_volume()
    single = ms.make_single()

    # ---------------------------------------------------------------- files, reader (no GUI)
    v5, v73 = ms.write_all(folder)
    ok(f"synthetic files written: {os.path.basename(v5)}, {os.path.basename(v73)}")
    for path, layout in ((v5, False), (v73, True)):
        f = mv.MesFile(path)
        _check(f.v73 == layout, f"{f.name}: layout detected as {'v7.3' if f.v73 else 'v5'}")
        _check([m.key for m in f.measurements] == ["DF0001", "DF0002"], f"{f.name}: headers {f.measurements}")
        m = f.measurements[0]
        _check(m.channels == ["UG", "UR"] and m.nplanes == T["planes"], f"{f.name}: channels/planes")
        _check(abs(m.px - T["px"]) < 1e-9 and abs(m.dz - T["dz"]) < 1e-9, f"{f.name}: px {m.px} dz {m.dz}")
        _check(m.objective == "Obj_16x", f"{f.name}: objective {m.objective!r}")
        _check(np.array_equal(m.load(), truth), f"{f.name}: DF0001 pixels differ from the truth")
        _check(np.array_equal(f.measurements[1].load(), single), f"{f.name}: DF0002 pixels differ")
        ok(f"{f.name}: {'MATLAB v7.3/HDF5' if layout else 'MATLAB v5'} headers and images read exactly")

    # ---------------------------------------------------------------- GUI
    import tkinter
    from tkinter import filedialog, messagebox
    App = mv.build_gui()
    errors = []
    out_dir = os.path.join(folder, "exports")
    os.makedirs(out_dir, exist_ok=True)
    # no dialogs during the test: answers are given here, error boxes fail the test
    filedialog.asksaveasfilename = lambda **k: os.path.join(out_dir, k.get("initialfile", "out.tif"))
    filedialog.askdirectory = lambda **k: out_dir
    messagebox.askyesno = lambda *a, **k: True
    messagebox.showinfo = lambda *a, **k: None
    messagebox.showwarning = lambda *a, **k: errors.append(("warning",) + a)
    messagebox.showerror = lambda *a, **k: errors.append(("error",) + a)

    try:
        app = App([v5, v73])
    except tkinter.TclError as ex:
        raise SelfTestError(f"could not open the main window (no display?): {ex}")

    def pump(n=5):
        for _ in range(n):
            app.update()
            time.sleep(0.02)

    def wait(cond, what, timeout=120):
        t0 = time.time()
        while not cond():
            if errors:
                raise SelfTestError(f"error dialog while waiting for {what}: {errors[-1]}")
            if time.time() - t0 > timeout:
                raise SelfTestError(f"timed out waiting for {what}")
            pump(2)
        pump(3)

    try:
        ok(f"main window opened ({app.title()})")
        _check(mv.__version__ in app.title(), "version missing from the window title")

        # overview table
        wait(lambda: len(app.rows) == 4, "the overview table (4 measurements)")
        rows = [app.tree.item(i, "values") for i in app.tree.get_children()]
        col = {c: k for k, c in enumerate(app.cols)}
        r0 = rows[0]
        _check(r0[col["Key"]] == "DF0001" and r0[col["Planes"]] == "6" and r0[col["Channels"]] == "UG,UR"
               and r0[col["Size (W×H)"]] == "64×48" and r0[col["Step"]] == "2 µm", f"overview row {r0}")
        _check(sorted(r[col["File"]] for r in rows) == sorted(["synthetic_v5.mes"] * 2 + ["synthetic_v73.mes"] * 2),
               "overview files")
        ok("overview table filled (2 files x 2 measurements, metadata columns correct)")

        def select(file_name, key):
            for iid, m in app.rows.items():
                if m.file.name == file_name and m.key == key:
                    app.tree.selection_set(iid)
                    break
            wait(lambda: app.m is not None and app.m.file.name == file_name and app.m.key == key
                 and app.vol is not None, f"{file_name} {key} to load")

        # ---- z-stack from the v5 file
        select("synthetic_v5.mes", "DF0001")
        _check(np.array_equal(app.vol, truth), "loaded stack differs from the truth")
        app._fill_meta()
        _check(len(app.meta.get_children()) > 10, "metadata tree empty")
        ok("z-stack loaded in the viewer, metadata tree filled")

        # projection
        app.mode.set("proj")
        app._proj_changed()
        app._render()
        exp = truth.max(0).astype(np.float32)
        _check(np.array_equal(app.img, exp), "max projection differs")
        for meth in ("mean", "sum", "std", "median", "min"):
            app.pmethod.set(meth)
            app._proj_changed()
            app._render()
            _check(np.allclose(app.img, mv.project(truth, meth)), f"{meth} projection differs")
        app.pmethod.set("max")
        app._proj_changed()
        app._render()
        ok("z-projection (max, mean, sum, std, median, min) matches")

        # polygon ROI across planes (cell square, half-pixel outside its edges)
        c = T["cell"]
        cell = [(c["x0"] - .5, c["y0"] - .5), (c["x1"] + .5, c["y0"] - .5),
                (c["x1"] + .5, c["y1"] + .5), (c["x0"] - .5, c["y1"] + .5)]
        npx = (c["x1"] - c["x0"] + 1) * (c["y1"] - c["y0"] + 1)
        app._polygon_done(cell)
        pump()
        first = [r for r in app.results if r["Channel"] == "UG"][-1]
        _check(first["Pixels"] == npx and abs(first["Mean"] - (T["bg_green"] + c["amp"])) < 1e-6,
               f"ROI on projection: {first['Pixels']} px, mean {first['Mean']}")
        _check(abs(first["Area"] - npx * T["px"] ** 2) < 1e-9, f"ROI area {first['Area']}")
        n0 = len(app.results)
        app.measure_across()
        rows = app.results[n0:]
        _check(len(rows) == T["planes"] * 2, f"{len(rows)} rows from 'across all planes'")
        for z in range(T["planes"]):
            g = [r for r in rows if r["Channel"] == "UG" and r["z_index"] == z][0]
            want = T["bg_green"] + (c["amp"] if z in c["planes"] else 0)
            _check(abs(g["Mean"] - want) < 1e-6, f"plane {z}: UG mean {g['Mean']} (expected {want})")
        ok(f"polygon ROI measured on the projection and across all {T['planes']} planes (means, area exact)")

        # ROI graph with ΔF/F
        app.open_plot()
        pw = app.plot_win
        pw.chan.set("UG")
        pw.dff.set(True)
        pw.f0.set("minimum of trace")          # F0 = background 100 -> ΔF/F of the cell = 300/100
        pw.refresh()
        pump()
        d = pw.data
        _check(d is not None and "ROI1_dFF" in d, f"graph data {list(d or {})}")
        _check(abs(d["ROI1_dFF"][3] - c["amp"] / T["bg_green"]) < 1e-6, f"ΔF/F at plane 3: {d['ROI1_dFF'][3]}")
        pw.kind.set("bars")
        pw.dff.set(False)
        pw.refresh()
        pump()
        _check(pw.data is not None and "UG_mean" in pw.data, "bar chart data")
        pw._close()
        ok("ROI graph: profile with ΔF/F and bar chart")

        # channel subtraction: k from an ROI on the red-only spot
        r = T["red_spot"]
        spot = [(r["x0"] - .5, r["y0"] - .5), (r["x1"] + .5, r["y0"] - .5),
                (r["x1"] + .5, r["y1"] + .5), (r["x0"] - .5, r["y1"] + .5)]
        app._polygon_done(spot)
        pump()
        app.k_from_roi()
        pump()
        k = float(app.sub_k.get())
        _check(abs(k - T["bleed"]) < 0.01, f"k from ROI = {k} (expected {T['bleed']})")
        _check(app.derived is not None and len(app.ch_names()) == 3, "no derived channel")
        resid = app.derived[:, r["y0"]:r["y1"] + 1, r["x0"]:r["x1"] + 1]
        _check(np.abs(resid).max() < 1.0, f"red spot not removed (max residual {np.abs(resid).max():.2f})")
        cellv = app.derived[3, c["y0"]:c["y1"] + 1, c["x0"]:c["x1"] + 1]
        _check(np.allclose(cellv, c["amp"], atol=1.0), "green cell changed by the subtraction")
        ok(f"channel subtraction: k = {k:.3f} from ROI, red spot removed, green cell kept")

        # noise reduction: preview of the visible plane, then Apply on the whole stack
        app.mode.set("plane")
        app.plane.set(0)
        app._plane_changed()
        app._render()
        app.nr_out.set(True)
        app.nr_method.set("Gaussian 2D (per plane)")
        app.nr_sigma.set("1.0")
        app._nr_changed()
        app.preview_nr()
        wait(lambda: app.preview is not None, "the noise-reduction preview")
        pv = app.preview["img"].copy()
        app._update_nr()
        wait(lambda: app.fvol is not None, "noise reduction of the stack")
        _check(np.allclose(pv, app.fvol[0], atol=1e-3), "preview differs from the applied result")
        z, ch, y, x = T["hot"][0]
        raw_hot = float(truth[z, ch, y, x])
        _check(app.fvol[z, ch, y, x] < 0.2 * raw_hot, f"hot pixel not removed ({app.fvol[z, ch, y, x]:.0f})")
        app.measure_all()
        last = [rr for rr in app.results if rr["Channel"] == "UG"][-1]
        _check("(raw)" in last["Source"], f"measurement source {last['Source']!r} (should be raw)")
        ok("noise reduction: outliers + Gaussian, preview identical to Apply, measurements stay raw")

        # exports
        app.export_stack()
        wait(lambda: app._busy == 0, "the stack TIFF export")
        app.export_stack(filtered=True)
        wait(lambda: app._busy == 0, "the noise-reduced TIFF export")
        app.mode.set("proj")
        app._proj_changed()
        app._render()
        app.export_projection()
        wait(lambda: app._busy == 0, "the projection TIFF export")
        import tifffile
        tifs = sorted(f for f in os.listdir(out_dir) if f.endswith(".tif"))
        _check(len(tifs) >= 3, f"TIFFs written: {tifs}")
        stack_tif = [f for f in tifs if not f.endswith("_denoised.tif") and "_max_" not in f][0]
        with tifffile.TiffFile(os.path.join(out_dir, stack_tif)) as tf:
            arr = tf.asarray()
            meta = tf.imagej_metadata or {}
        _check(arr.shape == (T["planes"], 3, T["height"], T["width"]), f"exported shape {arr.shape}")
        _check(abs(float(meta.get("spacing", 0)) - T["dz"]) < 1e-6, f"exported z spacing {meta.get('spacing')}")
        _check(np.allclose(arr[:, :2], truth), "exported pixels differ from the raw data")
        ok(f"TIFF export: stack (ZCYX, z-spacing {T['dz']} µm), noise-reduced stack and projection")

        # 3D view: built-in renderer (napari is never part of the installers)
        n_top = len([w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)])
        app.open_3d()          # napari if available (from source), otherwise built-in
        pump(10)
        tops = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)]
        if mv.napari_available():
            ok("3D view: napari is installed here and was launched")
            app.open_3d(force_builtin=True)
            pump(10)
            tops = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)]
        else:
            _check(len(tops) == n_top + 1, "3D view did not fall back to the built-in viewer")
        v3 = tops[-1]
        wait(lambda: not v3._busy and len(v3.ax_mip.images) > 0, "the rotating projection")
        v3._set_view(0, 90)
        wait(lambda: not v3._busy, "a side view")
        v3.render_ortho()
        v3.v_meth.set("Median 2D (per plane)")
        v3.apply_nr()
        wait(lambda: not v3._busy, "noise reduction in the 3D window")
        _check("active" in v3.nr_status.cget("text"), f"3D noise status {v3.nr_status.cget('text')!r}")
        v3.destroy()
        ok("3D view: built-in rotating projection, orthogonal views and its noise reduction work"
           + ("" if mv.napari_available() else " (napari not present, fallback used)"))

        # ---- the same stack from the v7.3/HDF5 file, and a single image
        select("synthetic_v73.mes", "DF0001")
        _check(np.array_equal(app.vol, truth), "v7.3 stack differs in the viewer")
        select("synthetic_v73.mes", "DF0002")
        app._render()
        s = T["single"]
        y0, y1, x0, x1 = s["square"]
        sq = [(x0 - .5, y0 - .5), (x1 + .5, y0 - .5), (x1 + .5, y1 + .5), (x0 - .5, y1 + .5)]
        app._polygon_done(sq)
        pump()
        last = app.results[-1]
        _check(abs(last["Mean"] - s["square_value"]) < 1e-6, f"single image ROI mean {last['Mean']}")
        ok("v7.3 file in the viewer; single image (DF0002) measured with a polygon")

        _check(not errors, f"error dialogs: {errors}")
    finally:
        try:
            app.destroy()
        except Exception:
            pass
