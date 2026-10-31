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

echo  KOSTI LICA: snyat' dannye riga i sravnit' s vanil'nym
echo.
"%BLENDER%" -b "%RIG%" --factory-startup -P "%TOOLS%tools\diag_face.py" -- --dump "%TOOLS%face_my.json"
echo.
if not exist "%TOOLS%face_van.json" goto :novan
echo  sravnenie s vanil'nym rigom:
echo.
%PY% "%TOOLS%tools\diag_face.py" --compare "%TOOLS%face_my.json" "%TOOLS%face_van.json"
goto :done
:novan
echo  Teper' snyat' tot zhe slepok s vanil'nym rigom (podstav' put' k .blend):
echo     "%BLENDER%" -b "d:\Temp\sims3-blender4+\s3_rig\vanilla_afemale.blend" --factory-startup -P "%TOOLS%tools\diag_face.py" -- --dump "%TOOLS%face_van.json"
echo  i zapusti 7_face.cmd snova - on srazu pokazhet sravnenie.
:done

echo.
echo  gotovo. okno mozhno zakryt'.
pause

:nopy
echo.
echo  !! Python 3 ne najden. Ustanovi Python i zapusti snova.
pause
exit /b 1
