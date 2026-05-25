@echo off
title TrustControl - Setup
echo.
echo  ============================================
echo   TrustControl - Installation
echo   Please wait, do not close this window
echo  ============================================
echo.

set WIN7=0
for /f "tokens=4 delims=. " %%i in ('ver') do if "%%i"=="6" set WIN7=1

if "%WIN7%"=="1" (
    echo  Windows 7 detected - using Python 3.8
    set PYTHON_VER=3.8.10
    set PYTHON_URL=https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
) else (
    echo  Windows 10/11 detected - using Python 3.11
    set PYTHON_VER=3.11.9
    set PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
)

set PY=
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python38\python.exe"
) do (
    if not defined PY (
        if exist %%P set PY=%%P
    )
)

if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set PY=python
)
if not defined PY (
    py --version >nul 2>&1
    if not errorlevel 1 set PY=py
)

if not defined PY (
    echo  [!] Python not found. Downloading Python %PYTHON_VER%...
    echo      This will take 1-2 minutes, please wait.
    echo.
    curl -L --progress-bar -o "%TEMP%\python_setup.exe" "%PYTHON_URL%"
    if errorlevel 1 (
        echo.
        echo  [ERROR] Failed to download Python.
        echo  Check your internet connection and try again.
        pause
        exit /b 1
    )
    echo  Installing Python %PYTHON_VER%...
    "%TEMP%\python_setup.exe" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0
    del "%TEMP%\python_setup.exe" >nul 2>&1

    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
    ) do (
        if not defined PY (
            if exist %%P set PY=%%P
        )
    )

    if not defined PY (
        echo.
        echo  [ERROR] Python installed but not found.
        echo  Close this window and run 1_SETUP.bat again.
        pause
        exit /b 1
    )
    echo  Python installed: %PY%
    echo.
)

echo  Python found: %PY%
echo.

echo  [1/3] Updating pip...
%PY% -m pip install --upgrade pip --quiet 2>nul

set REQ=requirements-monitor.txt
if "%WIN7%"=="1" set REQ=requirements-monitor-py38.txt

echo  [2/3] Installing dependencies...
%PY% -m pip install -r %REQ% --quiet
if errorlevel 1 (
    echo.
    echo  [!] Error during install. Trying one by one...
    %PY% -m pip install pyaudio --quiet
    %PY% -m pip install webrtcvad-wheels --quiet
    %PY% -m pip install requests --quiet
    %PY% -m pip install numpy --quiet
    %PY% -m pip install noisereduce --quiet
)

echo  [3/3] Checking modules...
%PY% -c "import pyaudio, webrtcvad, requests; print('  All modules OK')"
if errorlevel 1 (
    echo.
    echo  [!] Missing modules. Check connection and run again.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   Installation complete!
echo.
echo   Next step: double-click 3_RUN.bat
echo  ============================================
echo.
pause
