@echo off
REM 启动技术规范书审查本地后端栈（嵌入式 PostgreSQL + 规则服务 + 审查 MCP）
REM 端口：registry 8010，MCP 8775
setlocal
cd /d "%~dp0"
set PYTHONPATH=%~dp0py
set PYTHONIOENCODING=utf-8
REM 本地回环请求绕开系统代理
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
if "%TSR_DATA_DIR%"=="" set TSR_DATA_DIR=%~dp0data
"%~dp0.venv\Scripts\python.exe" -m technical_spec_review.local_stack
endlocal
