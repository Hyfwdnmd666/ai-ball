@echo off
rem ===================================================================
rem  ai-ball launcher
rem
rem  KEEP THIS FILE ASCII-ONLY.
rem  cmd.exe reads .bat files in the OEM codepage (GBK on Chinese
rem  Windows). UTF-8 Chinese comments here turn into mojibake, and the
rem  stray bytes get executed as commands -- it breaks loudly, and it
rem  breaks differently on every locale.
rem
rem  All user-facing Chinese comes from the Python scripts instead;
rem  they set encoding="replace" so they can never crash on it.
rem ===================================================================

chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem --- find a Python that actually has tkinter ---
rem  Search order:
rem    1. %AIBALL_PYTHON%  -- set this if your Python lives somewhere unusual
rem    2. python / py on PATH
rem    3. the usual per-user and system install locations
rem
rem  We *test* each candidate instead of trusting `where`, because some
rem  Windows setups ship a "python" stub from the Microsoft Store that
rem  just prints an error and exits.
rem
rem  `call` matters: if "python" resolves to a .bat shim (some setups do
rem  that), a plain invocation hands control to it and THIS script never
rem  continues -- it dies silently, with no error message at all.
set "PY="
if defined AIBALL_PYTHON set "PY=%AIBALL_PYTHON%"
if not defined PY call python -c "import tkinter" >nul 2>nul && set "PY=python"
if not defined PY call py -c "import tkinter" >nul 2>nul && set "PY=py"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PY if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY for /d %%D in ("C:\Python3*") do if not defined PY if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"

if not defined PY (
  echo.
  echo   [x] No Python with tkinter found. The ball cannot start.
  echo.
  echo   Install a standard Python 3.10+ from python.org. During setup,
  echo   tick "Add python.exe to PATH".
  echo.
  echo   Already have a Python somewhere unusual? Point us at it:
  echo.
  echo       setx AIBALL_PYTHON "C:\path\to\python.exe"
  echo.
  echo   ...then open a NEW terminal and run this script again.
  echo.
  pause
  exit /b 1
)

echo   Python: %PY%
echo.

rem --- self-check the brain first, so you never end up typing into a
rem     ball that silently cannot answer ---
"%PY%" brain.py --test
if errorlevel 1 (
  echo.
  echo   [!] Brain self-check failed. The ball will still start, but
  echo       nobody will answer you. Fix the key in brain.json, or run
  echo       the echo brain:  brain.py --brain echo
  echo.
  pause
)

echo   Starting ball...
start "" "%PY%" ball.py
echo   Starting brain...
start "" "%PY%" brain.py

echo.
echo   Both up. Close this window whenever -- it does not affect them.
echo   Right-click the ball for settings. Type in the panel to talk.
echo.
pause
