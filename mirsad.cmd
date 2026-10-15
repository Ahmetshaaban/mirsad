@echo off
rem Mirsad launcher (مرصد) — runs mirsad.py with any Python 3 on PATH.
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% "%~dp0mirsad.py" %*
