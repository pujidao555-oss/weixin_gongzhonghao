@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
echo ========================================
echo  贴图瀑布流总控  (CMD)
echo  工作目录: %CD%
echo  参数: %*
echo ========================================
python -u "%~dp0run_tietu_pipeline.py" %*
set EXITCODE=%ERRORLEVEL%
echo.
echo [CMD] 退出码=%EXITCODE%
echo.
echo 任务已结束，按任意键关闭本窗口…
pause >nul
exit /b %EXITCODE%
