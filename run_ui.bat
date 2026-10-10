@echo off
rem 광고청정기 화면 실행 (브라우저가 자동으로 열림, 종료: 이 창에서 Ctrl+C)
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python.exe -m ui.server
