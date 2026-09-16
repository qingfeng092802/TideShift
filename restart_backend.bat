@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Energy Dispatch - Backend Restart
echo ============================================
echo.

echo [1/3] Stopping old backend on port 8800 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8800" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo [2/3] Starting backend (chat_agent fix22 applied) ...
rem fix30：此前硬编码开发者本机绝对路径（C:\Users\...），换机必然失败。
rem 现与 start_web.bat 同策略：优先项目内 .venv，否则回退 PATH 中的 python。
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
start "energy-dispatch-backend" /min "%PY%" backend\server.py

echo [3/3] Health check ...
timeout /t 6 /nobreak >nul
curl -s -o nul -w "  /api/providers -> HTTP %%{http_code}  (401 = service up, auth required)\n" http://127.0.0.1:8800/api/providers

echo.
echo Done. Opening http://127.0.0.1:8800
start "" http://127.0.0.1:8800
endlocal
