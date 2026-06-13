# opengcp installer (Windows / PowerShell).
#
# opengcp is source-available (not published to PyPI). Install it from the git
# repository. This script tries pipx, then uv, then plain pip.
$ErrorActionPreference = "Stop"

$Repo = "git+https://github.com/cognis-digital/opengcp.git"

Write-Host "opengcp installer"
Write-Host "-----------------"

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (Test-Cmd pipx) {
    Write-Host "==> installing with pipx"
    pipx install $Repo
} elseif (Test-Cmd uv) {
    Write-Host "==> installing with uv"
    uv tool install $Repo
} elseif (Test-Cmd pip) {
    Write-Host "==> installing with pip"
    pip install --user $Repo
} elseif (Test-Cmd python) {
    Write-Host "==> installing with python -m pip"
    python -m pip install --user $Repo
} else {
    Write-Host "No pipx/uv/pip/python found. If you have cloned the repo, run:"
    Write-Host "    python -m pip install ."
    exit 1
}

Write-Host ""
Write-Host "Done. Try:  opengcp version"
Write-Host "Then:       opengcp serve --port 8085"
