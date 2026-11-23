@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FITAI Web Server

if not exist ".venv" (
    python -m venv .venv
)
call .venv\Scripts\activate
pip install -q -r requirements.txt
if not exist ".env" (
    copy .env.example .env >nul
)
start "" http://127.0.0.1:8000
python server.py
pause
