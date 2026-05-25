@echo off
set INSTALL_DIR=%~dp0
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

REM Создаём wrapper который запускает 3_RUN.bat из правильной папки
(
  echo @echo off
  echo cd /d "%INSTALL_DIR%"
  echo call "%INSTALL_DIR%3_RUN.bat"
) > "%STARTUP%\TrustControl.bat"

echo.
echo  Автозапуск настроен!
echo  Монитор будет запускаться при каждом включении ПК.
echo.
pause
