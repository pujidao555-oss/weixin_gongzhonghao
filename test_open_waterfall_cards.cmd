@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
echo ========================================
echo  单独测试：贴图瀑布流
echo  整卡ROI + 底栏OCR + 指纹（--method bar 可回退）
echo  快扫回顶 → 逐个打开卡片（不复制链接）
echo  工作目录: %CD%
echo  用法示例:
echo    test_open_waterfall_cards.cmd
echo    test_open_waterfall_cards.cmd --limit 3
echo    test_open_waterfall_cards.cmd --method bar --limit 3
echo    test_open_waterfall_cards.cmd --skip-scan --limit 5
echo  参数: %*
echo ========================================
python -u "%~dp0tools\test_open_waterfall_cards.py" %*
set EXITCODE=%ERRORLEVEL%
echo.
echo [CMD] 退出码=%EXITCODE%
echo 结果: tools\test_open_cards_result.json
echo 进度: tools\test_open_cards_progress.txt
echo.
echo 任务已结束，按任意键关闭本窗口…
pause >nul
exit /b %EXITCODE%
