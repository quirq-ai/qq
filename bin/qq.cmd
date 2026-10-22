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
rem QQ_PYTHON names the interpreter that runs qq. Without it, the first of py -3.14, python and
rem python3 that is 3.11.4 to 3.14 (qq's hashed lock has no wheels for 3.15 yet) runs it.
if defined QQ_PYTHON (
  set interpreter="%QQ_PYTHON%"
  "%QQ_PYTHON%" -I -c "import sys, venv; sys.exit(sys.version_info < (3, 11, 4))" 2>nul
  if errorlevel 1 (
    echo qq: QQ_PYTHON=%QQ_PYTHON% must be Python 3.11.4 or newer with its venv module. 1>&2
    exit /b 1
  )
) else (
  set interpreter=
  for %%P in ("py -3.14" "python" "python3") do if not defined interpreter (
    %%~P -I -c "import sys, venv; sys.exit(not (3, 11, 4) <= sys.version_info < (3, 15))" >nul 2>nul && set "interpreter=%%~P"
  )
)
if not defined interpreter (
  echo qq: needs Python 3.11.4 to 3.14 with its venv module, as py -3.14, python or python3 ^(or set QQ_PYTHON^). 1>&2
  exit /b 1
)
rem -I: nothing in the folder qq is run from, nor PYTHONPATH, is imported by the bootstrap.
%interpreter% -I "%depot%\src\qqdepot\bootstrap.py" "%depot%" %*
exit /b %errorlevel%
