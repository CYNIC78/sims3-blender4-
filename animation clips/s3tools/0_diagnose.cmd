@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ============================================================
rem  SETTINGS - poprav', esli puti drugie
rem ============================================================
set "BLENDER=D:\PortablePrograms\blender-4.4.3-windows-x64\blender.exe"
set "RIG=d:\Temp\sims3-blender4+\s3_rig\G9Female_1.2.blend"
rem ============================================================

set "TOOLS=%~dp0"
set "CLIPS=%TOOLS%.."
set "OUT=%TOOLS%..\..\s3_library"

set "PY=python"
where python >nul 2>nul
if not errorlevel 1 goto :havepy
where py >nul 2>nul
if errorlevel 1 goto :nopy
set "PY=py -3"
:havepy

echo  papka s klipami : %CLIPS%
echo  papka rezultata: %OUT%
echo.

echo  DIAGNOSTIKA: pochemu .glb vyhodit pustym ili tyazhelym
echo.
"%BLENDER%" -b "%RIG%" --factory-startup -P "%TOOLS%tools\\diag_export.py" -- --clips "%CLIPS%" %*

echo.
echo  gotovo. okno mozhno zakryt'.
pause

:nopy
echo.
echo  !! Python 3 ne najden. Ustanovi Python i zapusti snova.
pause
exit /b 1
