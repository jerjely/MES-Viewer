#!/bin/bash
# ============================================================
#  MES Viewer - environment installer for macOS (running from source)
#  Creates the conda environment "mesviewer" from environment.yml
#  (conda-forge only), optionally adds napari for the GPU 3D view,
#  and tests it. Run once (double-click in Finder); afterwards start
#  MES Viewer with start_mes_viewer.command.
#  Most users install the .pkg from the GitHub releases page instead.
#  If Finder says the file cannot be opened: in Terminal run
#     chmod +x install_mes_viewer_env.command start_mes_viewer.command
# ============================================================
ENV_NAME="mesviewer"
cd "$(dirname "$0")" || exit 1

echo
echo "=== MES Viewer environment installer ==="
echo

fail() { echo; echo "*** Installation failed - see the messages above. ***"; read -r -p "Press Return to close"; exit 1; }

[ -f environment.yml ] || { echo "environment.yml was not found next to this installer."; fail; }

# ---- find conda ---------------------------------------------------
CONDA_ROOT=""
for d in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/opt/miniconda3" "$HOME/opt/anaconda3" \
         "/opt/homebrew/Caskroom/miniforge/base" "/opt/miniconda3" "/opt/anaconda3" "/usr/local/miniconda3"; do
    if [ -z "$CONDA_ROOT" ] && [ -f "$d/etc/profile.d/conda.sh" ]; then CONDA_ROOT="$d"; fi
done
if [ -z "$CONDA_ROOT" ] && command -v conda >/dev/null 2>&1; then CONDA_ROOT="$(conda info --base 2>/dev/null)"; fi
if [ -z "$CONDA_ROOT" ] || [ ! -f "$CONDA_ROOT/etc/profile.d/conda.sh" ]; then
    echo "No Anaconda / Miniconda / Miniforge installation was found."
    echo "Install Miniforge first:  https://conda-forge.org/download/"
    echo "then run this installer again."
    fail
fi
echo "Using conda in: $CONDA_ROOT"
# shellcheck disable=SC1091
source "$CONDA_ROOT/etc/profile.d/conda.sh" || fail

# ---- create or update the environment -----------------------------
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    echo
    echo "Environment \"$ENV_NAME\" already exists."
    read -r -p "[R]ecreate it from scratch, [U]pdate it, or [Q]uit? " ans
    case "$ans" in
        [Rr]*) conda env remove -y -n "$ENV_NAME" || fail
               conda env create -n "$ENV_NAME" -f environment.yml || fail ;;
        [Uu]*) conda env update -n "$ENV_NAME" -f environment.yml || fail ;;
        *) exit 0 ;;
    esac
else
    echo
    echo "Creating environment \"$ENV_NAME\" from environment.yml - this can take a few minutes..."
    conda env create -n "$ENV_NAME" -f environment.yml || fail
fi

# ---- optional napari ------------------------------------------------
echo
read -r -p "Install napari for the GPU 3D volume view (several hundred MB)? [y/N] " ans
if [[ "$ans" =~ ^[Yy] ]]; then
    echo "Installing napari + Qt from conda-forge..."
    conda install -y -n "$ENV_NAME" --override-channels -c conda-forge napari pyqt \
        || echo "WARNING: napari installation failed - the built-in 3D view will be used instead."
fi

# ---- test -------------------------------------------------------------
conda activate "$ENV_NAME" || fail
echo
echo "Checking installation..."
python -c "import sys, numpy, scipy, matplotlib, tifffile, h5py, PIL, tkinter; print('  python', sys.version.split()[0], '| numpy', numpy.__version__, '| scipy', scipy.__version__, '| matplotlib', matplotlib.__version__, '| tifffile', tifffile.__version__, '| h5py', h5py.__version__, '| Tk', tkinter.TkVersion)" || fail
python -c "import napari; print('  napari', napari.__version__, 'OK')" 2>/dev/null || echo "  napari not installed - built-in 3D view will be used"
[ -f mes_viewer.py ] && python mes_viewer.py --version
chmod +x start_mes_viewer.command 2>/dev/null

echo
echo "=== Done. Start the viewer with start_mes_viewer.command ==="
read -r -p "Press Return to close"
