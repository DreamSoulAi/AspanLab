@echo off
REM Останавливает локальный ISSAI-воркер и туннель.
setlocal
cd /d "%~dp0\.."
echo Останавливаю ISSAI-воркер и туннель...
docker compose -f docker-compose.issai-local.yml down
echo Готово. Модель осталась в кэше — следующий запуск будет быстрым.
pause
