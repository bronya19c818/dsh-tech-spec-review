@echo off
REM End-to-end check: build a small DOCX, run a full review, verify the artifacts.
REM Needs a running rule server and a configured chat model; skips otherwise.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [e2e] environment missing, run: uv venv --python 3.12 .venv
  exit /b 1
)
".venv\Scripts\python.exe" tests\e2e_review.py %*
endlocal
