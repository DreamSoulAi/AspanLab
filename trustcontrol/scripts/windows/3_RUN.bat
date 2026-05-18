@echo off
chcp 65001 >nul
title TrustControl — Мониторинг касса

:: ── Читаем config.ini ──────────────────────────────────────
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v "^;" config.ini`) do (
    set "%%A=%%B"
)

:: Убираем пробелы
set API_URL=%API_URL: =%
set API_KEY=%API_KEY: =%
set LANGUAGE=%LANGUAGE: =%
set SILENCE=%SILENCE: =%
set VAD_LEVEL=%VAD_LEVEL: =%

:: ── Проверяем что API_KEY заполнен ────────────────────────
if "%API_KEY%"=="ВСТАВЬ_СЮДА_API_КЛЮЧ" (
    echo.
    echo  [!] API_KEY не настроен!
    echo  Открой config.ini и вставь ключ из личного кабинета.
    pause
    exit /b 1
)

if "%API_KEY%"=="" (
    echo  [!] API_KEY пустой! Открой 2_CONFIG.bat и заполни ключ.
    pause
    exit /b 1
)

:: ── Определяем команду python ──────────────────────────────
set PY=python
python --version >nul 2>&1
if %errorlevel% neq 0 set PY=py -3

:: ── Запускаем монитор ──────────────────────────────────────
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   TrustControl активен               ║
echo  ║   Слушаю микрофон...                 ║
echo  ║   Для остановки закрой это окно      ║
echo  ╚══════════════════════════════════════╝
echo.
echo  Сервер:  %API_URL%
echo  Ключ:    %API_KEY:~0,8%...
echo  Язык:    %LANGUAGE%
echo.

%PY% monitor.py ^
    --api-url "%API_URL%" ^
    --api-key "%API_KEY%" ^
    --language "%LANGUAGE%" ^
    --silence %SILENCE% ^
    --vad-level %VAD_LEVEL%

echo.
echo  [!] Монитор остановлен. Нажми любую клавишу для перезапуска...
pause >nul
call 3_RUN.bat
