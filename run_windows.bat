@echo off
rem PS Remover for Windows: installs the required packages if needed, then opens the GUI.
rem Arguments are passed on, e.g.  run_windows.bat remove photo.jpg --rect 10,10,100,50
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import PIL, win32com" >nul 2>nul || %PY% -m pip install --user -r requirements.txt
%PY% -m ps_remover %*
if errorlevel 1 pause
