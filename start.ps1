$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$server = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $server)) {
    throw '请先运行 python -m venv .venv 并安装 requirements.txt'
}
& $server -m uvicorn monitor.app:app --host 127.0.0.1 --port 8765
