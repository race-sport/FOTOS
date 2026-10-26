@echo off
setlocal
cd /d "%~dp0"
echo Preparando Fotos de Productos para Windows...
py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo Falta Python 3.11 de 64 bits. Descargalo desde el enlace de la guia.
  echo Instala Python y volve a abrir este archivo.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3.11 -m venv .venv
if errorlevel 1 goto error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error
echo.
echo Instalacion terminada. Abri 2_ABRIR.bat.
pause
exit /b 0
:error
echo No se pudo completar la instalacion. Revisa internet y copia el mensaje de error.
pause
exit /b 1
