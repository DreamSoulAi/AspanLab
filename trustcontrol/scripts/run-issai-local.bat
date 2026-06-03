@echo off
REM ============================================================
REM  ISSAI STT - локальный запуск на ПК (Windows)
REM  Двойной клик по этому файлу. Нужен установленный Docker Desktop.
REM ============================================================
setlocal
cd /d "%~dp0\.."

echo.
echo ============================================================
echo   ISSAI STT - запуск локального воркера + туннеля
echo ============================================================
echo.

REM --- Проверка Docker ---
docker version >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] Docker не запущен или не установлен.
  echo Установи Docker Desktop: https://www.docker.com/products/docker-desktop
  echo Запусти Docker Desktop и попробуй снова.
  echo.
  pause
  exit /b 1
)

echo [1/3] Собираю и поднимаю контейнеры (первый раз качает модель ~1.5GB, 3-5 мин)...
docker compose -f docker-compose.issai-local.yml up -d --build
if errorlevel 1 (
  echo [ОШИБКА] Не удалось запустить. Проверь что в Docker Desktop выделено 5-6GB RAM.
  pause
  exit /b 1
)

echo.
echo [2/3] Жду пока модель загрузится (до 5 минут)...
echo Проверяю health воркера...
set /a tries=0
:waitloop
timeout /t 10 /nobreak >nul
set /a tries+=1
curl -s -o nul -w "%%{http_code}" http://localhost:8010/health 2>nul | findstr "200 401" >nul
if errorlevel 1 (
  if %tries% lss 30 (
    echo   ... ещё не готов, жду (попытка %tries%/30)
    goto waitloop
  ) else (
    echo [ВНИМАНИЕ] Воркер долго не отвечает. Смотри логи:
    echo   docker compose -f docker-compose.issai-local.yml logs issai-worker
  )
) else (
  echo [OK] Воркер отвечает на http://localhost:8010
)

echo.
echo [3/3] Адрес туннеля для Render (ISSAI_WORKER_URL):
echo ------------------------------------------------------------
docker compose -f docker-compose.issai-local.yml logs cloudflared 2>nul | findstr "trycloudflare.com"
echo ------------------------------------------------------------
echo.
echo Если адрес выше пустой - подожди 20 сек и запусти:
echo   docker compose -f docker-compose.issai-local.yml logs cloudflared
echo.
echo В Render - Environment пропиши:
echo   ISSAI_WORKER_URL = https://....trycloudflare.com  (адрес выше)
echo   ISSAI_WORKER_KEY = trustlocal2026
echo.
echo Чтобы остановить позже: run-issai-stop.bat
echo.
pause
