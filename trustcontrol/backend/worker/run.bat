@echo off
chcp 65001 >nul

REM ════════════════════════════════════════════════════════════
REM  TrustControl — Запуск воркера
REM
REM  НАСТРОЙКА: замени значения ниже на свои
REM  API_URL  — адрес твоего сайта на Render
REM  API_KEY  — ключ точки из дашборда (Точки → скопировать ключ)
REM ════════════════════════════════════════════════════════════

set API_URL=https://ТВОЙ-САЙТ.onrender.com
set API_KEY=ВАШ_API_КЛЮЧ_ЗДЕСЬ

REM %~dp0 = папка где лежит этот .bat файл (всегда правильный путь)
set WORKER_DIR=%~dp0

REM ── Проверка конфига ─────────────────────────────────────────
if "%API_KEY%"=="ВАШ_API_КЛЮЧ_ЗДЕСЬ" (
    echo.
    echo [ОШИБКА] Не заполнен API_KEY!
    echo.
    echo Открой этот файл run.bat в Блокноте и замени:
    echo   ВАШ_API_КЛЮЧ_ЗДЕСЬ
    echo на ключ из дашборда (Точки - твоя точка - скопировать ключ^)
    echo.
    pause
    exit /b 1
)

if "%API_URL%"=="https://ТВОЙ-САЙТ.onrender.com" (
    echo.
    echo [ОШИБКА] Не заполнен API_URL!
    echo.
    echo Открой run.bat в Блокноте и замени:
    echo   https://ТВОЙ-САЙТ.onrender.com
    echo на настоящий адрес твоего сервера на Render
    echo.
    pause
    exit /b 1
)

REM ── Выбираем Python ──────────────────────────────────────────
py -3.13 --version >nul 2>&1
if not errorlevel 1 ( set PY=py -3.13 & goto :run )
py --version >nul 2>&1
if not errorlevel 1 ( set PY=py & goto :run )
python --version >nul 2>&1
if not errorlevel 1 ( set PY=python & goto :run )

echo [ОШИБКА] Python не найден! Сначала запусти setup.bat
pause
exit /b 1

:run
echo ╔══════════════════════════════════════════════════════════╗
echo ║         TrustControl — Мониторинг запущен               ║
echo ╚══════════════════════════════════════════════════════════╝
echo.
echo Сервер : %API_URL%
echo API KEY: %API_KEY:~0,8%...
echo.
echo Для остановки нажмите Ctrl+C
echo.

%PY% "%WORKER_DIR%monitor.py" --api-url "%API_URL%" --api-key "%API_KEY%"

echo.
echo Воркер остановлен.
pause
