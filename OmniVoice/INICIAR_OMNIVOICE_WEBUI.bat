@echo off
chcp 65001 >nul
title OmniVoice - Painel completo
cd /d "%~dp0"

echo ==============================================================
echo        Painel completo do OmniVoice (AMD Radeon RX 7600)
echo ==============================================================
echo.
echo O painel agora roda DENTRO do servico OmniVoice (porta 17495):
echo uma copia so do modelo na placa de video, e a mesma fila do VozLab.
echo Endereco: http://127.0.0.1:17495/painel
echo.

curl.exe -s -o nul -m 2 http://127.0.0.1:17495/health
if %ERRORLEVEL% NEQ 0 (
    echo [1/2] Ligando o servico OmniVoice em outra janela minimizada...
    start "OmniVoice Service 17495" /min cmd /c "%~dp0iniciar_omnivoice_service.bat"
) else (
    echo [1/2] O servico OmniVoice ja esta ligado.
)

echo [2/2] Esperando o painel ficar pronto...
set /a TENTATIVAS=0
:espera
set /a TENTATIVAS+=1
curl.exe -s -o nul -m 2 -f http://127.0.0.1:17495/painel/
if %ERRORLEVEL% EQU 0 goto pronto
if %TENTATIVAS% GEQ 90 goto falhou
timeout /t 1 /nobreak >nul
goto espera

:pronto
echo Painel pronto. Abrindo o navegador...
start "" "http://127.0.0.1:17495/painel/"
echo (O modelo so carrega na placa na primeira geracao.)
timeout /t 5 >nul
exit /b 0

:falhou
echo.
echo ==============================================================
echo O painel nao respondeu em 90 segundos.
echo Veja a janela "OmniVoice Service 17495" para o erro.
echo ==============================================================
pause
exit /b 1
