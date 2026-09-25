@echo off
rem PS Remover for Windows: double-click to start. The first run installs what it needs.
rem Arguments are passed on, for example:  run_windows.bat batch C:\photos --preset watermark
rem The Korean messages below need the UTF-8 code page.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "ps_remover\__main__.py" (
    echo.
    echo  프로그램 파일을 찾을 수 없습니다. ZIP 파일 안에서 바로 실행하면 이렇게 됩니다.
    echo  ZIP 파일을 오른쪽 클릭해 [압축 풀기] 또는 [모두 압축 풀기]를 한 뒤,
    echo  풀린 폴더 안의 run_windows.bat을 실행하세요.
    echo  Extract the ZIP file first, then run run_windows.bat from the extracted folder.
    echo.
    pause
    exit /b 1
)

rem The Microsoft Store stub named python.exe exists even without Python, so test by running it.
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (python -c "import sys" >nul 2>nul && set "PY=python")
if not defined PY (
    echo.
    echo  Python이 설치되어 있지 않습니다. 다운로드 페이지를 엽니다.
    echo  Python을 설치할 때 첫 화면의 "Add python.exe to PATH"를 체크하고,
    echo  설치가 끝나면 run_windows.bat을 다시 실행하세요.
    echo  Python is not installed: install it from the page that opens, then run this file again.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

%PY% -m ps_remover.launcher %*
if errorlevel 1 pause
endlocal
