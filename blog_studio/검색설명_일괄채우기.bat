@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 발행된 글 검색 설명 일괄 채우기...
python patch_search_desc.py
pause
