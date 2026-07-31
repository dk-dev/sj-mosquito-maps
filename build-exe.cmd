@echo off
setlocal

rem  Build the standalone Windows app: dist\sj-mosquito-maps.exe
rem
rem  Double-click this file, or run it from a terminal. It uses the project's
rem  .venv if one exists, installs PyInstaller if it is missing, and prints
rem  where the finished exe landed and how big it is.
rem
rem  build-exe.cmd /quiet   skips the closing "press any key" (for scripts/CI).

cd /d "%~dp0"

echo.
echo   Building sj-mosquito-maps.exe
echo   ==============================
echo.

rem --- Pick an interpreter --------------------------------------------------
rem  The venv is preferred and not merely convenient: PyInstaller freezes
rem  whatever is importable in the environment that runs it, so building with
rem  a different interpreter would quietly produce an exe with no pywebview
rem  and therefore no native window.
set "VENVPY=%~dp0.venv\Scripts\python.exe"
set "PYLAUNCH="
if exist "%VENVPY%" goto :have_python

set "VENVPY="
echo   NOTE: no .venv here - falling back to a system Python.
echo         For a reproducible build:  py -3 -m venv .venv
echo.
where py >nul 2>&1
if not errorlevel 1 set "PYLAUNCH=py -3"
if defined PYLAUNCH goto :have_python
where python >nul 2>&1
if not errorlevel 1 set "PYLAUNCH=python"
if defined PYLAUNCH goto :have_python

echo   ERROR: no Python found on this machine.
echo.
echo   Install Python 3.11 or newer from https://python.org, then run:
echo.
echo       py -3 -m venv .venv
echo       .venv\Scripts\python -m pip install -r requirements-build.txt
echo.
goto :fail_quiet

:have_python
call :py --version
if errorlevel 1 goto :fail

rem --- Make sure PyInstaller is available -----------------------------------
call :py -m PyInstaller --version >nul 2>&1
if not errorlevel 1 goto :build
echo   PyInstaller is not installed in this environment. Installing it...
echo.
call :py -m pip install -r "%~dp0requirements-build.txt"
if errorlevel 1 goto :fail

rem --- Build ----------------------------------------------------------------
:build
echo.
echo   Running PyInstaller (this takes a minute or two)...
echo.
call :py -m PyInstaller "%~dp0sj-mosquito-maps.spec" --noconfirm
if errorlevel 1 goto :fail

set "EXE=%~dp0dist\sj-mosquito-maps.exe"
if not exist "%EXE%" goto :fail

rem  %%~zF is the file's size in bytes; /1048576 makes that whole megabytes.
for %%F in ("%EXE%") do set "BYTES=%%~zF"
set /a MB=%BYTES%/1048576

echo.
echo   ==============================================================
echo    Done.
echo.
echo    Program:  %EXE%
echo    Size:     %MB% MB  (%BYTES% bytes)
echo.
echo    That one file is the whole app - copy it anywhere and
echo    double-click it. The spray archive is already inside, so it
echo    works offline; "Update maps" downloads anything new.
echo   ==============================================================
echo.
if /i "%~1"=="/quiet" exit /b 0
pause
exit /b 0

:fail
echo.
echo   BUILD FAILED. The last lines above say why.
echo.
:fail_quiet
if /i "%~1"=="/quiet" exit /b 1
pause
exit /b 1

rem  The single place that knows how to invoke Python, so the venv path (which
rem  may contain spaces) stays quoted while the launcher fallback stays
rem  unquoted. Only one of the two lines ever runs, and an "if" that does not
rem  fire leaves errorlevel alone - so the caller still sees the real result.
:py
if defined VENVPY "%VENVPY%" %*
if not defined VENVPY %PYLAUNCH% %*
goto :eof
