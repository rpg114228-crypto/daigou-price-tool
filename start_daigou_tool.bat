@echo off
cd /d "%~dp0"
echo Starting daigou price tool...
echo URL: http://127.0.0.1:8787
start "" http://127.0.0.1:8787
python -u daigou_price_backend.py
pause
