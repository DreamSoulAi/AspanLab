@echo off
chcp 65001 >nul
echo ╔══════════════════════════════════════════════════════════╗
echo ║         TrustControl — Установка воркера                 ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

REM %~dp0 = папка где лежит этот .bat файл (всегда правильный путь)
set WORKER_DIR=%~dp0

REM ── Проверяем Python ─────────────────────────────────────────
py -3.13 --version >nul 2>&1
if not errorlevel 1 (
    set PY=py -3.13
    goto :python_ok
)
py --version >nul 2>&1
if not errorlevel 1 (
    set PY=py
    goto :python_ok
)
python --version >nul 2>&1
if not errorlevel 1 (
    set PY=python
    goto :python_ok
)

echo [ОШИБКА] Python не найден!
echo Скачайте с https://www.python.org/downloads/
echo При установке отметьте "Add Python to PATH"
pause
exit /b 1

:python_ok
echo [OK] Python найден:
%PY% --version
echo.

REM ── Обновляем pip ────────────────────────────────────────────
echo Обновляю pip...
%PY% -m pip install --upgrade pip --quiet

REM ── Устанавливаем зависимости ────────────────────────────────
echo Устанавливаю зависимости...
%PY% -m pip install -r "%WORKER_DIR%requirements-monitor.txt"
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить зависимости!
    echo Попробуйте запустить от имени администратора (ПКМ → Запуск от имени администратора)
    pause
    exit /b 1
)

echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║  Установка завершена! Теперь настрой run.bat:            ║
echo ║                                                          ║
echo ║  1. Открой run.bat в Блокноте                            ║
echo ║  2. Вставь свой API_KEY (из дашборда → Точки)            ║
echo ║  3. Вставь API_URL (адрес твоего сайта на Render)        ║
echo ║  4. Сохрани и запусти run.bat двойным кликом             ║
echo ╚══════════════════════════════════════════════════════════╝
echo.
pause
