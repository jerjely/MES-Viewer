#!/bin/bash
# ============================================================
#  MES Viewer starter for macOS (running from source, for development)
#  Double-click in Finder. Uses the conda environment "mesviewer"
#  (made by install_mes_viewer_env.command) if it exists, otherwise
#  conda's base environment or the python3 on PATH.
#  Most users install the .pkg from the GitHub releases page instead.
#  If Finder says the file cannot be opened: in Terminal run
#     chmod +x start_mes_viewer.command
# ============================================================
ENV_NAME=""          # leave empty to auto-pick "mesviewer"
cd "$(dirname "$0")" || exit 1

if [ ! -f mes_viewer.py ]; then
    echo "mes_viewer.py was not found next to this file: $(pwd)"
    read -r -p "Press Return to close"; exit 1
fi

CONDA_ROOT=""
for d in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/opt/miniconda3" "$HOME/opt/anaconda3" \
         "/opt/homebrew/Caskroom/miniforge/base" "/opt/miniconda3" "/opt/anaconda3" "/usr/local/miniconda3"; do
    if [ -z "$CONDA_ROOT" ] && [ -f "$d/etc/profile.d/conda.sh" ]; then CONDA_ROOT="$d"; fi
done

PY=python3
if [ -n "$CONDA_ROOT" ]; then
    # shellcheck disable=SC1091
    source "$CONDA_ROOT/etc/profile.d/conda.sh"
    if [ -z "$ENV_NAME" ] && conda env list | awk '{print $1}' | grep -qx mesviewer; then ENV_NAME=mesviewer; fi
    conda activate "${ENV_NAME:-base}" || { echo "Could not activate conda environment ${ENV_NAME:-base}"; read -r -p "Press Return"; exit 1; }
    PY=python
fi

if ! "$PY" -c "import numpy, scipy, matplotlib, tifffile, h5py, tkinter" >/dev/null 2>&1; then
    echo "Installing missing packages from requirements.txt ..."
    "$PY" -m pip install -r requirements.txt || { echo "Package installation failed."; read -r -p "Press Return"; exit 1; }
fi

"$PY" mes_viewer.py "$@" || { echo; echo "MES Viewer exited with an error - see the messages above."; read -r -p "Press Return to close"; }
