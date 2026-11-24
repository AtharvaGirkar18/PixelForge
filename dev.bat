@echo off
REM  dev.bat  --  Run the full stack locally WITHOUT Docker/Kubernetes.
REM  Requires: Python 3.10+, pip packages (see below).
REM  This lets you iterate on code quickly before building container images.
REM
REM  What it starts (in separate windows):
REM    1.  fakeredis  (in-process Redis stand-in on 127.0.0.1:6379)
REM    2.  FastAPI server on :8000
REM    3.  Worker 1  (pod-w1)
REM    4.  Worker 2  (pod-w2)
REM    5.  Worker 3  (pod-w3)
REM
REM  Usage:
REM    dev.bat           # start 3 workers  (default)
REM    dev.bat 5         # start N workers
REM    dev.bat stop      # kill all dev processes
REM
REM  Deps (one-time):
REM    pip install fastapi "uvicorn[standard]" python-multipart redis pillow numpy ^
REM               kubernetes "fakeredis[lua]"

set N_WORKERS=%1
if "%N_WORKERS%"=="" set N_WORKERS=3
if "%N_WORKERS%"=="stop" goto :stop

pip show fakeredis >nul 2>&1 || (
  echo Installing Python dependencies...
  pip install -q fastapi "uvicorn[standard]" python-multipart redis pillow numpy kubernetes "fakeredis[lua]"
)

set REDIS_URL=redis://127.0.0.1:6379/0
set STORAGE_PATH=%~dp0.devstorage

echo.
echo [1/3] Starting fake Redis on :6379
start "FakeRedis" cmd /k python scripts\dev_redis.py
timeout /t 2 /nobreak >nul

echo [2/3] Starting FastAPI on http://127.0.0.1:8000
start "PixelForge API" cmd /k "set REDIS_URL=%REDIS_URL%&& set STORAGE_PATH=%STORAGE_PATH%&& python -m uvicorn api.main:app --port 8000 --reload"
timeout /t 2 /nobreak >nul

echo [3/3] Starting %N_WORKERS% worker(s)
for /L %%i in (1,1,%N_WORKERS%) do (
  start "Worker %%i" cmd /k "set REDIS_URL=%REDIS_URL%&& set STORAGE_PATH=%STORAGE_PATH%&& set HOSTNAME=pod-w%%i&& python -m worker.worker"
  timeout /t 1 /nobreak >nul
)

echo.
echo ================================================
echo  Dev environment running with %N_WORKERS% workers.
echo  Open: http://127.0.0.1:8000
echo  Stop: dev.bat stop
echo ================================================
goto :eof

:stop
echo Stopping all PixelForge dev processes...
taskkill /fi "WindowTitle eq FakeRedis*"    /f >nul 2>&1
taskkill /fi "WindowTitle eq PixelForge*"   /f >nul 2>&1
taskkill /fi "WindowTitle eq Worker*"       /f >nul 2>&1
echo Done.
