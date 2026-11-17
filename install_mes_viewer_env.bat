@echo off
REM ============================================================
REM  MES Viewer - environment installer (running from source)
REM  Creates a separate conda environment "mesviewer" from
REM  environment.yml (conda-forge only), optionally adds napari
REM  for the GPU 3D view, tests it, and can add a desktop shortcut.
REM  Run once; afterwards start with start_mes_viewer.bat
REM  Needs in the same folder: environment.yml, mes_viewer.py,
REM                            start_mes_viewer.bat
REM ============================================================
setlocal
set "ENV_NAME=mesviewer"
cd /d "%~dp0"

echo.
echo === MES Viewer environment installer ===
echo.

if not exist "environment.yml" (
    echo environment.yml was not found next to this installer.
    pause
    exit /b 1
)

REM ---- find conda ------------------------------------------------
set "CONDA_ROOT="
for %%D in ("%USERPROFILE%\miniforge3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\miniforge3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\miniforge3" "C:\anaconda3" "C:\miniconda3") do (
    if not defined CONDA_ROOT if exist "%%~D\Scripts\activate.bat" set "CONDA_ROOT=%%~D"
)
if not defined CONDA_ROOT (
    echo No Anaconda / Miniconda / Miniforge installation was found.
    echo Install Miniforge first:  https://conda-forge.org/download/
    echo then run this installer again.
    pause
    exit /b 1
)
echo Using conda in: %CONDA_ROOT%
call "%CONDA_ROOT%\Scripts\activate.bat"
if errorlevel 1 goto :fail

REM ---- existing environment? ---------------------------------------
set "ENV_EXISTS="
if exist "%CONDA_ROOT%\envs\%ENV_NAME%\python.exe" set "ENV_EXISTS=1"
if exist "%USERPROFILE%\.conda\envs\%ENV_NAME%\python.exe" set "ENV_EXISTS=1"
if not defined ENV_EXISTS goto :create

echo.
echo Environment "%ENV_NAME%" already exists.
choice /c RUQ /n /m "[R]ecreate it from scratch, [U]pdate it, or [Q]uit? "
if errorlevel 3 exit /b 0
if errorlevel 2 goto :update
echo Removing old environment...
call conda env remove -y -n %ENV_NAME%
if errorlevel 1 goto :fail

:create
echo.
echo Creating environment "%ENV_NAME%" from environment.yml - this can take a few minutes...
call conda env create -n %ENV_NAME% -f environment.yml
if errorlevel 1 goto :fail
goto :napari

:update
echo.
echo Updating "%ENV_NAME%" from environment.yml...
call conda env update -n %ENV_NAME% -f environment.yml
if errorlevel 1 goto :fail

:napari
echo.
choice /c YN /n /m "Install napari for the GPU 3D volume view (several hundred MB)? [Y/N] "
if errorlevel 2 goto :test
echo Installing napari + Qt from conda-forge...
call conda install -y -n %ENV_NAME% --override-channels -c conda-forge napari pyqt
if errorlevel 1 echo WARNING: napari installation failed - the built-in 3D view will be used instead.

:test
call conda activate %ENV_NAME%
if errorlevel 1 goto :fail
echo.
echo Checking installation...
python -c "import sys, numpy, scipy, matplotlib, tifffile, h5py, PIL, tkinter; print('  python', sys.version.split()[0]); print('  numpy', numpy.__version__, '| scipy', scipy.__version__, '| matplotlib', matplotlib.__version__, '| tifffile', tifffile.__version__, '| h5py', h5py.__version__); print('  Tk', tkinter.TkVersion)"
if errorlevel 1 goto :fail
python -c "import matplotlib; matplotlib.use('TkAgg'); from matplotlib.widgets import PolygonSelector; from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar; print('  matplotlib TkAgg backend OK')"
if errorlevel 1 goto :fail
python -c "import napari; print('  napari', napari.__version__, 'OK')" 2>nul || echo   napari not installed - built-in 3D view will be used
if exist "mes_viewer.py" (
    python mes_viewer.py --version
) else (
    echo   NOTE: mes_viewer.py not found next to this installer.
)

REM ---- optional desktop shortcut -----------------------------------
echo.
choice /c YN /n /m "Create a desktop shortcut to MES Viewer (from source)? [Y/N] "
if errorlevel 2 goto :done
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\MES Viewer (source).lnk'); $s.TargetPath='%~dp0start_mes_viewer.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0icons\mes_viewer.ico'; $s.WindowStyle=7; $s.Save()"
echo   Shortcut "MES Viewer (source)" created on the desktop (you can drag .mes files onto it).

:done
echo.
echo === Done. Start the viewer with start_mes_viewer.bat ===
pause
exit /b 0

:fail
echo.
echo *** Installation failed - see the messages above. ***
pause
exit /b 1
