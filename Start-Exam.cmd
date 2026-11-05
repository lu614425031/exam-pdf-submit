@echo off
cd /d "%~dp0"
py -3 server.py --bind 0.0.0.0 %*
pause
