@echo off
REM ============================================================
REM  build.bat  --  Build and tag Docker images, load into
REM  Minikube (or Docker Desktop) and apply all k8s manifests.
REM
REM  Usage:
REM    build.bat            # build + load + apply (full deploy)
REM    build.bat build      # build images only
REM    build.bat apply      # apply manifests only (images already loaded)
REM    build.bat logs       # stream all pod logs
REM    build.bat reset      # delete namespace and recreate from scratch
REM ============================================================

set ACTION=%1
if "%ACTION%"=="" set ACTION=all
cd /d "%~dp0"

set API_IMAGE=image-api:1.0
set WORKER_IMAGE=image-worker:1.0
set NS=imgproc

if "%ACTION%"=="build" goto :build
if "%ACTION%"=="apply" goto :apply
if "%ACTION%"=="logs"  goto :logs
if "%ACTION%"=="reset" goto :reset
if "%ACTION%"=="all"   goto :all

echo Unknown action: %ACTION%
echo Usage: build.bat [build^|apply^|logs^|reset^|all]
exit /b 1

:all
  call :build
  call :apply
  goto :eof

:build
  echo.
  echo ==============================
  echo Building Docker images...
  echo ==============================
  docker build -t %API_IMAGE% -f api\Dockerfile .
  if errorlevel 1 ( echo FAILED: api image & exit /b 1 )
  docker build -t %WORKER_IMAGE% -f worker\Dockerfile .
  if errorlevel 1 ( echo FAILED: worker image & exit /b 1 )
  echo.
  echo Detecting local Kubernetes...
  for /f "delims=" %%C in ('kubectl config current-context') do set KUBE_CONTEXT=%%C
  if /I "%KUBE_CONTEXT%"=="minikube" (
    echo Minikube context active. Loading images...
    minikube image load %API_IMAGE%
    if errorlevel 1 ( echo FAILED: load API image into Minikube & exit /b 1 )
    minikube image load %WORKER_IMAGE%
    if errorlevel 1 ( echo FAILED: load worker image into Minikube & exit /b 1 )
  ) else if /I "%KUBE_CONTEXT%"=="docker-desktop" (
    echo Docker Desktop Kubernetes context active. Importing local images...
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\load_docker_desktop_images.ps1
    if errorlevel 1 ( echo FAILED: import images into Docker Desktop Kubernetes & exit /b 1 )
  ) else (
    echo Unsupported or missing Kubernetes context: %KUBE_CONTEXT%
    echo Select the Minikube or docker-desktop context before building.
    exit /b 1
  )
  goto :eof

:apply
  echo.
  echo ==============================
  echo Applying Kubernetes manifests...
  echo ==============================
  kubectl delete hpa image-worker-hpa -n %NS% --ignore-not-found
  kubectl apply -f k8s\00-base.yaml
  kubectl apply -f k8s\10-redis.yaml
  kubectl apply -f k8s\20-api.yaml
  kubectl apply -f k8s\30-worker.yaml
  kubectl rollout restart deployment/image-api deployment/image-worker -n %NS%
  echo.
  echo Waiting for API pod readiness (up to 90 s)...
  kubectl rollout status deployment/image-api -n %NS% --timeout=90s
  kubectl rollout status deployment/image-worker -n %NS% --timeout=90s
  echo.
  echo ==============================
  echo Deploy complete!
  echo ==============================
  echo.
  echo Access the UI:
  for /f "delims=" %%C in ('kubectl config current-context') do set KUBE_CONTEXT=%%C
  if /I "%KUBE_CONTEXT%"=="minikube" (
    echo   minikube service image-api -n %NS%
  ) else (
    echo   kubectl port-forward svc/image-api 30080:8000 -n %NS%
    echo   then open http://localhost:30080
  )
  echo.
  echo Useful kubectl commands:
  echo   kubectl get pods -n %NS% -w
  echo   kubectl top pods -n %NS%
  echo   kubectl get deployment/image-worker -n %NS% -w
  echo   kubectl delete pod ^<worker-pod^> -n %NS%   ^<-- self-healing demo
  goto :eof

:logs
  kubectl logs -n %NS% -l app=image-worker --all-containers --follow --prefix=true
  goto :eof

:reset
  echo.
  echo WARNING: This will delete the '%NS%' namespace and all data.
  set /p CONFIRM=Type YES to continue:
  if NOT "%CONFIRM%"=="YES" ( echo Cancelled. & goto :eof )
  kubectl delete namespace %NS% --ignore-not-found
  kubectl delete pv imgproc-storage-pv --ignore-not-found
  echo.
  echo Namespace deleted. Run 'build.bat apply' to redeploy.
  goto :eof
