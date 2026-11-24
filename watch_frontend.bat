@echo off
REM ============================================================
REM  watch_frontend.bat -- Watches api\static\ and auto-syncs
REM  all edits directly into the live Kubernetes pod in real time.
REM  No Docker rebuilds or pod restarts needed!
REM ============================================================

echo Starting Frontend Live Sync...
python -u scripts\sync_frontend.py
pause
