@echo off
rem Starts the BD Stock Tracker dashboard and opens it in your browser.
cd /d "%~dp0"
python run.py serve --open
pause
