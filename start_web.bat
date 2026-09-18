@echo off
rem 储能调度系统 Web 版一键启动（使用 PATH 中的 python，不含个人路径）
rem 双击后自动打开浏览器 http://127.0.0.1:8800
rem 补充：优先使用项目虚拟环境（若存在），否则回退 PATH python
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
start "" http://127.0.0.1:8800
%PY% backend\server.py
pause
