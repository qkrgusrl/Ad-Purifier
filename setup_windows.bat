@echo off
rem Windows 11 개발 환경 구성 (Python 3.12 필요)
chcp 65001 >nul
cd /d %~dp0
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv\Scripts\python.exe -m pip install laya playwright
.venv\Scripts\python.exe -m playwright install chromium
