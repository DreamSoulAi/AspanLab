@echo off
chcp 65001 >nul
title TrustControl — Установка
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   TrustControl — Установка           ║
echo  ║   Подождите, не закрывайте окно      ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── Определяем версию Windows ────────────────────────────────
set WIN7=0
for /f "tokens=4 delims=. " %%i in ('ver') do if "%%i"=="6" set WIN7=1

if "%WIN7%"=="1" (
    echo  Обнаружена Windows 7 — используем Python 3.8
    set PYTHON_VER=3.8.10
    set PYTHON_URL=https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
    set PYTHON_DIR38=%LOCALAPPDATA%\Programs\Python\Python38\python.exe
) else (
    echo  Обнаружена Windows 10/11 — используем Python 3.11
    set PYTHON_VER=3.11.9
    set PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
)

:: ── Ищем Python в стандартных папках ────────────────────────
set PY=
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python310\python.exe"
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

:: ── Если Python не найден — скачиваем подходящую версию ─────
if not defined PY (
    echo  [!] Python не найден. Скачиваем Python %PYTHON_VER%...
    echo      Это займёт 1-2 минуты, не закрывайте окно.
    echo.
    curl -L --progress-bar -o "%TEMP%\python_setup.exe" "%PYTHON_URL%"
    if errorlevel 1 (
        echo.
        echo  [ОШИБКА] Не удалось скачать Python.
        echo  Проверьте подключение к интернету и запустите снова.
        pause
        exit /b 1
    )
    echo  Устанавливаем Python %PYTHON_VER%...
    "%TEMP%\python_setup.exe" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0
    del "%TEMP%\python_setup.exe" >nul 2>&1

    :: Ищем снова после установки
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
        echo  [ОШИБКА] Python установлен, но не найден.
        echo  Закройте окно и запустите 1_SETUP.bat снова.
        pause
        exit /b 1
    )
    echo  Python установлен: %PY%
    echo.
)

echo  Найден Python: %PY%
echo.

:: ── Обновляем pip ────────────────────────────────────────────
echo  [1/3] Обновляем pip...
%PY% -m pip install --upgrade pip --quiet 2>nul

:: ── Определяем нужный requirements файл ─────────────────────
set REQ=requirements-monitor.txt
if "%WIN7%"=="1" set REQ=requirements-monitor-py38.txt

:: ── Устанавливаем зависимости ────────────────────────────────
echo  [2/3] Устанавливаем зависимости...
%PY% -m pip install -r %REQ% --quiet
if errorlevel 1 (
    echo.
    echo  [!] Ошибка при установке. Пробуем по одному...
    %PY% -m pip install pyaudio --quiet
    %PY% -m pip install webrtcvad-wheels --quiet
    %PY% -m pip install requests --quiet
    %PY% -m pip install numpy --quiet
)

:: ── Финальная проверка ────────────────────────────────────────
echo  [3/3] Проверка...
%PY% -c "import pyaudio, webrtcvad, requests; print('  Все модули OK')"
if errorlevel 1 (
    echo.
    echo  [!] Не хватает модулей. Проверьте подключение и запустите снова.
    pause
    exit /b 1
)

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Установка завершена!               ║
echo  ║                                      ║
echo  ║   Следующий шаг:                     ║
echo  ║   Дважды кликните на 3_RUN.bat       ║
echo  ╚══════════════════════════════════════╝
echo.
pause
