#!/bin/bash
# Playwright 설치(최초 1회만 실행하면 됩니다) — 맥용 런처
echo "===================================================="
echo " Playwright 설치 (최초 1회만 실행하면 됩니다)"
echo "===================================================="
echo
# 최신 맥의 Homebrew Python은 기본적으로 pip install을 막는다(externally-managed-environment)
# → 실패하면 --break-system-packages로 한 번 더 시도(이 프로그램 전용 도구라 안전).
pip3 install playwright || pip3 install --break-system-packages playwright
echo
echo "Chromium 브라우저 설치 중..."
python3 -m playwright install chromium
echo
echo "===================================================="
echo " 설치 완료! 이제 발행하기.command를 실행하세요."
echo "===================================================="
read -p "Enter 키를 누르면 창이 닫힙니다..."
