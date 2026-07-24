@echo off
chcp 65001 >nul
echo ====================================================
echo  Playwright 설치 (최초 1회만 실행하면 됩니다)
echo ====================================================
echo.
pip install playwright --break-system-packages
echo.
echo Chromium 브라우저 설치 중...
playwright install chromium
echo.
echo ====================================================
echo  설치 완료! 이제 발행하기.bat을 실행하세요.
echo ====================================================
pause
