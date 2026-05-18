@echo off
chcp 65001 >nul
title TrustControl — Мониторинг касса

:: ── Читаем config.ini (пропускаем секции [..] и комментарии ;) ─
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v /r "^;" "config.ini" ^| findstr /v /r "^\["`) do (
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
    echo  ╔══════════════════════════════════════╗
    echo  ║   API-ключ не настроен!              ║
    echo  ║                                      ║
    echo  ║   1. Войди в личный кабинет          ║
    echo  ║   2. Открой раздел "Точки"           ║
    echo  ║   3. Нажми кнопку скопировать ключ  ║
    echo  ║   4. Запусти 2_CONFIG.bat            ║
    echo  ║   5. Вставь ключ: правая кнопка →   ║
    echo  ║      Вставить, затем Сохранить       ║
    echo  ╚══════════════════════════════════════╝
    echo.
    pause
    exit /b 1
)

if "%API_KEY%"=="" (
    echo  [!] API_KEY пустой! Запусти 2_CONFIG.bat и заполни ключ.
    pause
    exit /b 1
)

:: ── Находим Python ──────────────────────────────────────────
set PY=
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
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
    echo  [!] Python не найден. Запусти 1_SETUP.bat сначала.
    pause
    exit /b 1
)

:: ── Цикл с автоперезапуском ────────────────────────────────
:loop
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
echo  [!] Монитор остановлен. Автоперезапуск через 10 секунд...
echo      Закрой это окно если хочешь остановить.
timeout /t 10 /nobreak >nul
goto :loop
