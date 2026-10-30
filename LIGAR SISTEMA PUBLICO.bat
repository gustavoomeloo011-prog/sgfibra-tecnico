@echo off
title EBF Tecnico - Ligar acesso publico
cd /d "%~dp0"
echo.
echo Este comando vai liberar o endereco publico do EBF Tecnico.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\set_public_access.ps1" -Disabled 0
echo.
pause
