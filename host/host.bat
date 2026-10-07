@echo off
rem Chrome launches this, not python directly: it can't resolve a .py
rem through PATHEXT. Runs from the project root, which is also where
rem Claude Code keeps the sessions follow-ups resume.
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -u host.py %*
) else (
    python -u host.py %*
)
