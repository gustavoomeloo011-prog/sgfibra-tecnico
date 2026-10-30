@echo off
setlocal
title EBF Tecnico Essencial - Somente Local
cd /d "%~dp0"

echo.
echo ============================================================
echo   EBF TECNICO ESSENCIAL - ACESSO SOMENTE NESTE COMPUTADOR
echo ============================================================
echo.
echo Cloudflare, Render e ngrok permanecem desativados.
echo Nenhum aparelho externo podera acessar este servidor.
echo.

set "SYSTEM_DISABLED=0"
set "EBF_SECURE_COOKIE=0"
set "PORT=8000"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Criando o ambiente Python local...
    py -3 -m venv "%~dp0.venv"
    if errorlevel 1 goto :error
)

echo Verificando as dependencias...
"%PYTHON_EXE%" -m pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 goto :error

echo.
echo Servidor iniciado em:
echo.
echo     http://127.0.0.1:8000
echo.
echo Somente este computador consegue acessar.
echo Para desligar, feche esta janela ou pressione CTRL+C.
echo.

start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"
"%PYTHON_EXE%" -m uvicorn app_fastapi.main:app --host 127.0.0.1 --port 8000 --workers 1
goto :end

:error
echo.
echo Nao foi possivel iniciar o servidor local.
pause

:end
endlocal
