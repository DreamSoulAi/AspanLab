@echo off
chcp 65001 >nul
title TrustControl — Установка
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   TrustControl — Установка           ║
echo  ║   Подождите, не закрывайте окно      ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── Ищем Python в стандартных папках ────────────────────────
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

:: Проверяем через PATH если не нашли по прямому пути
if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set PY=python
)
if not defined PY (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set PY=py -3
)

:: ── Если Python не найден — скачиваем и устанавливаем ────────
if not defined PY (
    echo  [!] Python не найден. Скачиваем Python 3.13...
    echo      Это займёт 1-2 минуты, не закрывайте окно.
    echo.
    curl -L --progress-bar -o "%TEMP%\python_setup.exe" "https://www.python.org/ftp/python/3.13.0/python-3.13.0-amd64.exe"
    if errorlevel 1 (
        echo.
        echo  [ОШИБКА] Не удалось скачать Python.
        echo  Проверьте подключение к интернету и запустите снова.
        pause
        exit /b 1
    )
    echo  Устанавливаем Python автоматически...
    :: /passive = тихая установка без диалогов, PrependPath = добавить в PATH
    "%TEMP%\python_setup.exe" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
    del "%TEMP%\python_setup.exe" >nul 2>&1

    :: Ищем снова после установки
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) do (
        if not defined PY (
            if exist %%P set PY=%%P
        )
    )

    if not defined PY (
        echo.
        echo  [ОШИБКА] Python установлен, но не найден.
        echo  Закройте это окно и запустите 1_SETUP.bat снова.
        pause
        exit /b 1
    )
    echo  Python установлен: %PY%
    echo.
)

echo  Найден Python: %PY%
echo.

:: ── Обновляем pip ────────────────────────────────────────────
echo  [1/4] Обновляем pip...
%PY% -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo  [!] pip не обновился — продолжаем с текущей версией.
)

:: ── Устанавливаем основные зависимости ──────────────────────
echo  [2/4] Устанавливаем зависимости...
%PY% -m pip install -r requirements-monitor.txt
if errorlevel 1 (
    echo.
    echo  [ОШИБКА] Не удалось установить зависимости.
    echo  Проверьте подключение к интернету и запустите снова.
    pause
    exit /b 1
)

:: ── Проверяем pyaudio отдельно ────────────────────────────────
echo  [3/4] Проверяем аудио-модуль...
%PY% -c "import pyaudio; print('  OK: pyaudio', pyaudio.__version__)" 2>nul
if errorlevel 1 (
    echo  Pyaudio не загрузился, пробуем переустановить...
    %PY% -m pip install --upgrade pyaudio
    %PY% -c "import pyaudio" 2>nul
    if errorlevel 1 (
        echo.
        echo  [!] Pyaudio не установился. Попробуйте:
        echo      1. Установите Visual C++ Redistributable:
        echo         https://aka.ms/vs/17/release/vc_redist.x64.exe
        echo      2. Запустите 1_SETUP.bat снова.
        pause
        exit /b 1
    )
)

:: ── Финальная проверка ────────────────────────────────────────
echo  [4/4] Проверка...
%PY% -c "import pyaudio, webrtcvad, numpy, requests; print('  Все модули OK')"
if errorlevel 1 (
    echo  [ОШИБКА] Что-то не установилось. Запустите 1_SETUP.bat снова.
    pause
    exit /b 1
)

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Установка завершена!               ║
echo  ║                                      ║
echo  ║   Следующий шаг:                     ║
echo  ║   1. Двойной клик на 2_CONFIG.bat   ║
echo  ║   2. Вставь API-ключ из кабинета    ║
echo  ║      (правая кнопка → Вставить)     ║
echo  ║   3. Файл → Сохранить → Закрыть     ║
echo  ║   4. Двойной клик на 3_RUN.bat      ║
echo  ╚══════════════════════════════════════╝
echo.
pause
