@echo off
title TrustControl - Автозапуск
set INSTALL_DIR=%~dp0

echo.
echo  Настройка автозапуска через Планировщик задач...
echo.

schtasks /delete /tn "TrustControl" /f >nul 2>&1

schtasks /create ^
  /tn "TrustControl" ^
  /tr "cmd /c cd /d \"%INSTALL_DIR%\" && call \"%INSTALL_DIR%3_RUN.bat\"" ^
  /sc ONLOGON ^
  /delay 0000:30 ^
  /rl HIGHEST ^
  /f >nul

if errorlevel 1 (
    echo  [!] Не удалось создать задачу. Запустите от имени администратора.
    pause
    exit /b 1
)

echo  Готово! Мониторинг будет запускаться автоматически
echo  через 30 секунд после входа в Windows.
echo.
echo  Чтобы отключить автозапуск:
echo    Пуск -^> Планировщик задач -^> TrustControl -^> Удалить
echo.
pause
