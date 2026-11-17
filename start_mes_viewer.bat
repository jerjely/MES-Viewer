@echo off
REM ============================================================
REM  MES Viewer starter (running from source, for development)
REM  - put this file next to mes_viewer.py
REM  - double-click to start, or drag .mes file(s) onto it
REM  Most users install MES Viewer with the Windows installer from
REM  the GitHub releases page instead and never need this file.
REM ============================================================
setlocal

REM ---- settings ------------------------------------------------
REM Name of the conda environment to use. Leave empty to auto-pick:
REM "mesviewer" (made by install_mes_viewer_env.bat) if it exists,
REM otherwise "base" / the Python on PATH.
set "ENV_NAME="
REM ------------------------------------------------------------

cd /d "%~dp0"

if not exist "mes_viewer.py" (
    echo mes_viewer.py was not found next to this .bat file:
    echo   %~dp0
    pause
    exit /b 1
)

REM ---- find and activate conda (Anaconda / Miniconda / Miniforge)
set "CONDA_ACT="
for %%D in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%USERPROFILE%\miniforge3" "%LOCALAPPDATA%\anaconda3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3" "C:\anaconda3" "C:\miniconda3") do (
    if not defined CONDA_ACT if exist "%%~D\Scripts\activate.bat" set "CONDA_ACT=%%~D\Scripts\activate.bat"
)

if defined CONDA_ACT if not defined ENV_NAME (
    for %%A in ("%CONDA_ACT%") do set "CONDA_ROOT=%%~dpA.."
)
if defined CONDA_ACT if not defined ENV_NAME (
    if exist "%CONDA_ROOT%\envs\mesviewer\python.exe" set "ENV_NAME=mesviewer"
    if exist "%USERPROFILE%\.conda\envs\mesviewer\python.exe" set "ENV_NAME=mesviewer"
)

if defined CONDA_ACT (
    if defined ENV_NAME (
        call "%CONDA_ACT%" %ENV_NAME%
    ) else (
        call "%CONDA_ACT%"
    )
    if errorlevel 1 (
        echo Could not activate conda environment "%ENV_NAME%".
        pause
        exit /b 1
    )
)

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Anaconda/Miniconda or Python 3.10+ first.
    pause
    exit /b 1
)

REM ---- make sure the required packages are installed ----------
python -c "import numpy, scipy, matplotlib, tifffile, h5py, tkinter" >nul 2>nul
if errorlevel 1 (
    echo Installing missing packages from requirements.txt ...
    python -m pip install --user -r requirements.txt
    python -c "import numpy, scipy, matplotlib, tifffile, h5py, tkinter" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Package installation failed - see the messages above.
        pause
        exit /b 1
    )
)

REM ---- start the viewer (any dropped .mes files are passed on) --
python "%~dp0mes_viewer.py" %*
if errorlevel 1 (
    echo.
    echo MES Viewer exited with an error - see the messages above.
    pause
)

endlocal
