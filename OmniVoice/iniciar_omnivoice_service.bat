@echo off
title OmniVoice Service 17495
chcp 65001 >nul
echo ========================================================
echo   Iniciando o servico OmniVoice (porta 17495)
echo   Ambiente isolado: %~dp0.venv
echo ========================================================
rem O disfarce do ROCm (HSA_OVERRIDE_GFX_VERSION) vem do VozLab.exe, que escolhe pela placa.
rem Rodando este arquivo sozinho numa AMD RX 7000, defina antes:  set HSA_OVERRIDE_GFX_VERSION=11.0.0
if not defined HIP_VISIBLE_DEVICES set HIP_VISIBLE_DEVICES=0
cd /d "%~dp0"
call "%~dp0.venv\Scripts\activate.bat"
python "%~dp0omnivoice_service.py"
pause
