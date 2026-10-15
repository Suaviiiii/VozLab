@echo off
rem Compila VozLab.exe (na pasta do projeto) a partir de vozlab\VozLab.cs (csc do .NET Framework, ja vem no Windows).
rem Uso: vozlab\compilar_vozlab.bat [pasta_de_saida]   (sem argumento: grava na pasta do projeto)
cd /d "%~dp0.."
set "SAIDA=%~1"
if "%SAIDA%"=="" set "SAIDA=%CD%"
"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /optimize+ ^
  /win32icon:vozlab\VozLab.ico /out:"%SAIDA%\VozLab.exe" ^
  /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Management.dll ^
  vozlab\VozLab.cs
if errorlevel 1 (echo FALHOU a compilacao & exit /b 1)
echo OK: %SAIDA%\VozLab.exe
