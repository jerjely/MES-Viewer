# PyInstaller recipe for a standalone MES Viewer (no Python needed on the target computer).
#   Windows: dist/MES-Viewer/MES-Viewer.exe      macOS: dist/MES-Viewer.app      Linux: dist/MES-Viewer/MES-Viewer
# Build with:
#   python -m PyInstaller --noconfirm --distpath dist --workpath build install/mes_viewer.spec
# The release workflow (.github/workflows/build-installers.yml) runs this on Windows and macOS,
# runs the built app's self-test, and packs the result into
#   MES-Viewer-<version>-Windows-setup.exe and MES-Viewer-<version>-macOS-<arch>.pkg.
#
# Not included on purpose: napari (large, Qt/OpenGL based, fragile to freeze). Without it the app
# uses its built-in 3D viewer; see the README for adding napari when running from source.
import os
import re
import sys

HERE = os.path.abspath(os.path.join(SPECPATH, ".."))
MAC = sys.platform == "darwin"
WIN = sys.platform.startswith("win")

a = Analysis(
    [os.path.join(HERE, "mes_viewer.py")],
    pathex=[HERE],
    datas=[
        (os.path.join(HERE, "icons", "mes_viewer_256.png"), "icons"),
        (os.path.join(HERE, "icons", "mes_viewer_512.png"), "icons"),
        (os.path.join(HERE, "icons", "mes_viewer.ico"), "icons"),
        (os.path.join(HERE, "README.md"), "."),
        (os.path.join(HERE, "LICENSE"), "."),
    ],
    hiddenimports=[
        # modules of this app that are imported lazily
        "version", "mes_selftest", "mes_synthetic",
        # readers / writers used on demand
        "scipy.io.matlab", "scipy.ndimage", "h5py", "tifffile",
        # GUI backend and figure exporters (savefig picks these at run time)
        "matplotlib.backends.backend_tkagg", "matplotlib.backends.backend_agg",
        "matplotlib.backends.backend_pdf", "matplotlib.backends.backend_svg",
        "mpl_toolkits.axes_grid1.anchored_artists",
        # matplotlib's Tk toolbar draws its button icons through Pillow's Tk bridge, which
        # PyInstaller does not detect (the built app crashed without these; found by the self-test)
        "PIL.ImageTk", "PIL._tkinter_finder",
    ],
    excludes=[
        "napari", "vispy", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy",
        "IPython", "jupyter", "notebook", "pytest", "mat73", "cv2",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="MES-Viewer",
    console=False,                        # windowed app; the self-test reports via selftest.txt + exit code
    icon=os.path.join(HERE, "icons", "mes_viewer.icns" if MAC else "mes_viewer.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MES-Viewer", upx=False)

if MAC:
    VERSION = os.environ.get("MESV_VERSION", "1.0.0")
    _m = re.match(r"\d+(\.\d+){0,2}", VERSION)        # Info.plist wants digits only (no "-test")
    SHORT = _m.group(0) if _m else "1.0.0"
    app = BUNDLE(
        coll, name="MES-Viewer.app", icon=os.path.join(HERE, "icons", "mes_viewer.icns"),
        bundle_identifier="io.github.jerjely.mesviewer",
        info_plist={
            "CFBundleName": "MES Viewer",
            "CFBundleDisplayName": "MES Viewer",
            "CFBundleShortVersionString": SHORT,
            "CFBundleVersion": SHORT,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "© Gergely Szarka, MIT License",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Femtonics MES file",
                "CFBundleTypeExtensions": ["mes"],
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
            }],
        },
    )
