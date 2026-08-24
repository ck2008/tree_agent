@echo off
rem Launch Tree Agent without a console window hanging around.
rem Uses pythonw when available so no black box stays on screen.
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul && (
  start "" pythonw -m tree_agent
) || (
  python -m tree_agent
)
endlocal
