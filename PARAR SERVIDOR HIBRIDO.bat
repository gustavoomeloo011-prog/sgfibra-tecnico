@echo off
taskkill /IM ngrok.exe /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
echo Servidor FastAPI e tunel encerrados.
pause
