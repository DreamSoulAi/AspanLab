@echo off
chcp 65001 >nul
title TrustControl — Установка
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   TrustControl — Установка           ║
echo  ║   Подождите, не закрывайте окно      ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── Проверяем Python ───────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    py -3 --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo  [!] Python не найден. Скачиваем...
        echo.
        powershell -Command "Start-Process 'https://www.python.org/ftp/python/3.13.0/python-3.13.0-amd64.exe' -Wait" 2>nul
        curl -L -o python_setup.exe "https://www.python.org/ftp/python/3.13.0/python-3.13.0-amd64.exe"
        echo  Запускаем установщик Python...
        echo  ВАЖНО: поставьте галочку "Add Python to PATH"
        start /wait python_setup.exe InstallAllUsers=1 PrependPath=1
        del python_setup.exe
    )
)

:: ── Определяем команду python ──────────────────────────────
set PY=python
python --version >nul 2>&1
if %errorlevel% neq 0 set PY=py -3

:: ── Устанавливаем зависимости ──────────────────────────────
echo  [1/3] Обновляем pip...
%PY% -m pip install --upgrade pip --quiet

echo  [2/3] Устанавливаем зависимости...
%PY% -m pip install -r requirements-monitor.txt --quiet

echo  [3/3] Готово!
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Установка завершена!               ║
echo  ║                                      ║
echo  ║   Теперь:                            ║
echo  ║   1. Открой config.ini               ║
echo  ║   2. Вставь API_KEY из кабинета      ║
echo  ║   3. Запусти 2_RUN.bat               ║
echo  ╚══════════════════════════════════════╝
echo.
pause
