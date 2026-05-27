@echo off
REM Local build of TrustControl-Monitor.exe
REM Run on a Windows machine with Python 3.11

cd /d "%~dp0"

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install pipwin
python -m pipwin install pyaudio
python -m pip install -r requirements-monitor.txt
python -m pip install pyinstaller==6.10.0

echo Building .exe...
pyinstaller ^
  --onefile ^
  --name TrustControl-Monitor ^
  --console ^
  --collect-binaries webrtcvad ^
  --collect-data noisereduce ^
  --hidden-import=numpy ^
  --hidden-import=pyaudio ^
  --hidden-import=webrtcvad ^
  --hidden-import=noisereduce ^
  --hidden-import=requests ^
  monitor.py

echo.
echo Done. Output: dist\TrustControl-Monitor.exe
pause
