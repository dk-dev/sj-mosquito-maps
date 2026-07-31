@echo off
REM Double-click this to open the spray timelapse.
REM
REM Prefers the project venv; falls back to the py launcher so the file still
REM works on a fresh clone before `py -3.14 -m venv .venv` has been run.
REM Pass --refresh to re-scrape the district's page first:  view-map.cmd --refresh

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where py >nul 2>&1
    if errorlevel 1 (
        echo Could not find a Python launcher. Install Python 3.14, then run:
        echo     py -3.14 -m venv .venv
        echo     .venv\Scripts\python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
    set "PY=py -3.14"
    echo No .venv found - using the system Python.
)

echo Starting the map...
%PY% app.py %*

REM Keep the window open if something went wrong, so the error is readable
REM rather than vanishing with the console.
if errorlevel 1 pause
