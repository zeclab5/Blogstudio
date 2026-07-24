#!/bin/bash
# Claude(API) 글 생성용 anthropic 패키지 설치 — 맥용 런처
echo "Claude(API) 글 생성을 쓰려면 anthropic 패키지가 필요합니다."
echo "설치를 시작합니다..."
echo
pip3 install anthropic || pip3 install --break-system-packages anthropic
echo
echo "설치가 끝났습니다. 이 창을 닫아도 됩니다."
read -p "Enter 키를 누르면 창이 닫힙니다..."
