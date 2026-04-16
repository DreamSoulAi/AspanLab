@echo off
chcp 65001 >nul
echo ╔══════════════════════════════════════════════════════════╗
echo ║         TrustControl — Установка воркера                 ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

REM ── Проверяем Python 3.13 ────────────────────────────────────
py -3.13 --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python 3.13 не найден!
    echo Скачайте с https://www.python.org/downloads/
    echo При установке отметьте "Add Python to PATH"
    pause
    exit /b 1
)

echo [OK] Python 3.13 найден
py -3.13 --version
echo.

REM ── Обновляем pip ────────────────────────────────────────────
echo Обновляю pip...
py -3.13 -m pip install --upgrade pip --quiet

REM ── Устанавливаем зависимости ────────────────────────────────
echo Устанавливаю зависимости...
py -3.13 -m pip install -r ..\..\requirements-monitor.txt
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить зависимости!
    echo Попробуйте запустить этот файл от имени администратора.
    pause
    exit /b 1
)

echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║  Установка завершена успешно!                            ║
echo ║                                                          ║
echo ║  Для запуска воркера:                                    ║
echo ║    Дважды кликните на start.bat                          ║
echo ║    или запустите run.bat                                 ║
echo ╚══════════════════════════════════════════════════════════╝
echo.
pause
