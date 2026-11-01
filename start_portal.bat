@echo off
title SKIDATA Operations Portal
cd /d "%~dp0"
echo ===================================================
echo   Starting SKIDATA Control Plane & Operations Portal
echo   URL:         http://127.0.0.1:8000
echo   Credentials: admin / Makan@2026!
echo ===================================================
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8000
.\.venv\Scripts\python.exe portal_server.py
pause
