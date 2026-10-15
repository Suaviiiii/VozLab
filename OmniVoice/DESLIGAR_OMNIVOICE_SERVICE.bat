@echo off
title Encerrar OmniVoice Service
chcp 65001 >nul
echo ========================================================
echo   Encerrando o OmniVoice (servico, trabalhador de GPU
echo   e painel antigo da porta 7860, se estiver aberto)
echo ========================================================
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*omnivoice_service.py*' -or $_.CommandLine -like '*omnivoice.cli.demo*' } | ForEach-Object { taskkill /T /F /PID $_.ProcessId 2>&1 | Out-Null; Write-Host ('Processo encerrado: PID ' + $_.ProcessId) }"
echo Concluido. VRAM e processos liberados com sucesso.
pause
