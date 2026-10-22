@echo off
rem qq bootstrap for Windows (cmd and PowerShell). Put this directory on PATH, like depot_tools.
rem
rem The first run makes a launcher environment from this depot checkout's commit, under
rem %QQ_HOME% (default %LOCALAPPDATA%\qq; see bootstrap.py). The launcher reads the current
rem repo's infra/repo.toml and runs the qq version it pins, installing that version first.
rem On Windows only qq sync, qq fetch and qq sync project run so far; the other commands need
rem a POSIX shell, so use WSL for them.
setlocal
for %%I in ("%~dp0..") do set "depot=%%~fI"
rem QQ_PYTHON names the interpreter that runs qq; the default is the py launcher's Python 3.14.
if defined QQ_PYTHON (set interpreter="%QQ_PYTHON%") else (set interpreter=py -3.14)
%interpreter% -I -c "import sys, venv; sys.exit(sys.version_info < (3, 11, 4))" 2>nul
if errorlevel 1 (
  echo qq: %interpreter% must be Python 3.11.4 or newer with its venv module ^(set QQ_PYTHON to use another^). 1>&2
  exit /b 1
)
rem -I: nothing in the folder qq is run from, nor PYTHONPATH, is imported by the bootstrap.
%interpreter% -I "%depot%\src\qqdepot\bootstrap.py" "%depot%" %*
exit /b %errorlevel%
