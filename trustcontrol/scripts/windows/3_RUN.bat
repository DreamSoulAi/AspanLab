@echo off
title TrustControl - Monitor

for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v /r "^;" "config.ini" ^| findstr /v /r "^\["`) do (
    set "%%A=%%B"
)

set API_URL=%API_URL: =%
set API_KEY=%API_KEY: =%
set LANGUAGE=%LANGUAGE: =%
set SILENCE=%SILENCE: =%
set VAD_LEVEL=%VAD_LEVEL: =%

if "%API_KEY%"=="ВСТАВЬ_СЮДА_API_КЛЮЧ" (
    echo.
    echo  [!] API key not set!
    echo      Open config.ini in Notepad and paste your key.
    echo.
    pause
    exit /b 1
)

if "%API_KEY%"=="" (
    echo  [!] API_KEY is empty! Open config.ini and set your key.
    pause
    exit /b 1
)

set PY=
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
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
    echo  [!] Python not found. Run 1_SETUP.bat first.
    pause
    exit /b 1
)

:loop
echo.
echo  ============================================
echo   TrustControl - Active
echo   Listening to microphone...
echo   Close this window to stop.
echo  ============================================
echo.
echo  Server:   %API_URL%
echo  Key:      %API_KEY:~0,8%...
echo  Language: %LANGUAGE%
echo.

%PY% monitor.py ^
    --api-url "%API_URL%" ^
    --api-key "%API_KEY%" ^
    --language "%LANGUAGE%" ^
    --silence %SILENCE% ^
    --vad-level %VAD_LEVEL%

echo.
echo  [!] Monitor stopped. Restarting in 10 seconds...
echo      Close this window to stop completely.
timeout /t 10 /nobreak >nul
goto :loop
