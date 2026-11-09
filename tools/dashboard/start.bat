@echo off
rem 交易面板 · 一键启动（只读数据服务，不写任何文件）
rem 用法：双击本文件 → 浏览器自动打开 http://127.0.0.1:8770/
cd /d "%~dp0..\.."
python tools\dashboard\server.py --port 8770 --open
pause
