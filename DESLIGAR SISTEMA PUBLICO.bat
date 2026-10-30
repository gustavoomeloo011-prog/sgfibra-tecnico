@echo off
title EBF Tecnico - Desligamento de emergencia
cd /d "%~dp0"
echo.
echo Este comando vai bloquear imediatamente o gateway publico.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\set_public_access.ps1" -Disabled 1
echo.
pause
