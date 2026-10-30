@echo off
set "PATH=C:\Program Files\nodejs;%PATH%"
cd /d "%~dp0"
echo Abrindo a autorizacao segura da Cloudflare...
call node_modules\.bin\wrangler.cmd login
echo.
echo Resultado da autorizacao:
call node_modules\.bin\wrangler.cmd whoami
echo.
pause
