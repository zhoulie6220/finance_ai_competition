@echo off
chcp 65001 >nul
REM ============================================================
REM  一键启动工作台（后端 + 前端各开一个窗口）
REM
REM  用法：双击本文件
REM  关闭：把弹出的两个黑色窗口都关掉即可
REM
REM  为什么要写成脚本：这两个服务每次重启都要敲两条命令、
REM  还要记两个端口号。演示前后要反复起停，写成双击一次省事，
REM  也免得「忘了先起后端，前端一片空白」。
REM ============================================================

cd /d "%~dp0"

echo 正在启动后端和前端，请稍候…
echo.

REM 先检查虚拟环境在不在——不在的话直接说清楚，别让前端起来后一片空白
if not exist "backend\.venv\Scripts\python.exe" (
    echo [错误] 找不到 backend\.venv
    echo        需要先建虚拟环境：
    echo            cd backend
    echo            python -m venv .venv
    echo            .venv\Scripts\python.exe -m pip install -r requirements.lock.txt
    echo.
    pause
    exit /b 1
)

REM 后端：8000 端口
start "后端 API" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

REM 前端：vite 会自己挑一个没被占用的端口，看它打印出来的那个
start "前端 工作台" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo 两个窗口都起来了。
echo.
echo   后端  http://127.0.0.1:8000
echo   前端  看「前端 工作台」那个窗口里打印的 Local 地址
echo         （通常是 http://localhost:5173，被占用时会往后跳）
echo.
echo 如果前端窗口提示 npm 找不到，先在那个窗口里跑一次：
echo     npm install
echo.
echo 关闭：把两个黑窗口都关掉。
echo.
pause
