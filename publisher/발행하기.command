#!/bin/bash
# 오늘의 글 발행 — 맥용 런처(윈도우 발행하기.bat 대응)
cd "$(dirname "$0")"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
python3 publish_today.py
read -p "Enter 키를 누르면 창이 닫힙니다..."
