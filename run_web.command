#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "먼저 WEB_DEPLOY.md에 따라 .venv 환경과 requirements.txt를 설치해주세요."
  exit 1
fi
exec .venv/bin/python web_app.py
