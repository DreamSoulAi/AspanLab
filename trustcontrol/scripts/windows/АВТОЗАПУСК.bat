@echo off
:: Добавляет 3_RUN.bat в автозапуск Windows
:: После этого монитор будет стартовать при включении компьютера

set SCRIPT_DIR=%~dp0
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

copy "%SCRIPT_DIR%3_RUN.bat" "%STARTUP%\TrustControl.bat" >nul
echo.
echo  ✓ Автозапуск настроен!
echo  Монитор будет запускаться при включении компьютера.
echo.
pause
