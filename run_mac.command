#!/bin/bash
# PS Remover for macOS: installs the required packages if needed, then opens the GUI.
# Arguments are passed on, e.g.  ./run_mac.command remove photo.jpg --rect 10,10,100,50
cd "$(dirname "$0")" || exit 1
PY=python3
if ! "$PY" -c "import tkinter" 2>/dev/null; then
  echo "이 Python에는 Tkinter가 없습니다. python.org에서 Python을 설치하거나 'brew install python-tk'를 실행하세요."
  exit 1
fi
if ! "$PY" -c "import PIL" 2>/dev/null; then
  "$PY" -m pip install --user -r requirements.txt || {
    echo "패키지를 설치하지 못했습니다. README의 설치 방법(가상 환경)을 참고하세요."
    exit 1
  }
fi
exec "$PY" -m ps_remover "$@"
