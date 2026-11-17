#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"

if ! command -v exiftool >/dev/null 2>&1; then
  echo "ExifTool не найден. Установите Homebrew, затем выполните: brew install exiftool"
  echo "https://formulae.brew.sh/formula/exiftool"
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.txt
(sleep 2; open "http://127.0.0.1:5000") &
exec .venv/bin/python app.py
