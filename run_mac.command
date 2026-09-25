#!/bin/bash
# PS Remover for macOS: double-click to start. The first run installs what it needs.
# Arguments are passed on, for example:  ./run_mac.command batch ~/Pictures/photos --preset watermark
cd "$(dirname "$0")" || exit 1

if [ ! -f ps_remover/__main__.py ]; then
  echo "프로그램 파일을 찾을 수 없습니다. ZIP 파일의 압축을 푼 폴더 안에서 run_mac.command를 실행하세요."
  exit 1
fi

# Prefer the python.org installation. Apple's /usr/bin/python3 is skipped: its old Tk
# can crash, and on a Mac without developer tools it pops up an install dialog instead.
PY=""
for candidate in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
                 /usr/local/bin/python3 /opt/homebrew/bin/python3 "$(command -v python3)"; do
  if [ -n "$candidate" ] && [ "$candidate" != /usr/bin/python3 ] && [ -x "$candidate" ] \
     && "$candidate" -c "import tkinter" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "쓸 수 있는 Python이 없습니다. 다운로드 페이지를 엽니다."
  echo "python.org에서 Python을 설치한 뒤 run_mac.command를 다시 실행하세요."
  open "https://www.python.org/downloads/"
  exit 1
fi

exec "$PY" -m ps_remover.launcher "$@"
