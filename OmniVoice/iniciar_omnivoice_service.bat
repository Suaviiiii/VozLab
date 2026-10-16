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
rem Sem o .venv, o "python" do PATH podia ser o do VozLab (venv_rocm, transformers 4.x) e o
rem OmniVoice quebrava so na 1a geracao (HiggsAudioV2TokenizerModel). Agora para aqui, com aviso.
if not exist "%~dp0.venv\Scripts\python.exe" goto sem_venv
"%~dp0.venv\Scripts\python.exe" -c "import sys, torch, transformers; sys.exit(0 if int(transformers.__version__.split('.')[0]) >= 5 else 1)" 2>nul
if errorlevel 1 goto venv_incompleto
call "%~dp0.venv\Scripts\activate.bat"
"%~dp0.venv\Scripts\python.exe" "%~dp0omnivoice_service.py"
pause
exit /b

:sem_venv
echo.
echo [ERRO] O ambiente do OmniVoice nao foi instalado:
echo        falta a pasta %~dp0.venv
echo        Siga o passo 4 do INSTALAR.md (obrigatorio para usar o OmniVoice;
echo        na NVIDIA GTX 1000 o OmniVoice e o unico modelo).
echo.
pause
exit /b 1

:venv_incompleto
echo.
echo [ERRO] O ambiente do OmniVoice esta incompleto (falta o PyTorch ou o
echo        transformers 5 ou mais novo). Refaca o passo 4 do INSTALAR.md:
echo          .venv\Scripts\python.exe -m pip install -e .
echo.
pause
exit /b 1
