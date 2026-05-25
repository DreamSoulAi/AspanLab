@echo off
set SCRIPT_DIR=%~dp0
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

copy "%SCRIPT_DIR%3_RUN.bat" "%STARTUP%\TrustControl.bat" >nul
echo.
echo  Autostart configured!
echo  Monitor will start automatically when PC turns on.
echo.
pause
