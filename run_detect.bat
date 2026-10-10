@echo off
rem 사용: run_detect.bat http://127.0.0.1:8765/   (결과: 이 폴더의 result.json, report.json)
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python.exe -m detector.main %1 --out .
.venv\Scripts\python.exe eval\score.py result.json
