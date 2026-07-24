#!/bin/bash
# 발행된 글 검색 설명(searchDescription) 일괄 채우기 — 맥용 런처
cd "$(dirname "$0")"
echo "발행된 글 검색 설명 일괄 채우기..."
python3 patch_search_desc.py
read -p "Enter 키를 누르면 창이 닫힙니다..."
