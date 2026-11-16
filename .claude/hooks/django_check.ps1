# PostToolUse (Edit|Write): after a change to app Python code, run Django's quick system check. Exit 2 shows Claude the errors.
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
$path = [string]$data.tool_input.file_path
if (-not $path) { exit 0 }
$p = $path.Replace('/', '\')
if ($p -notmatch '(^|\\)(core|hvac)\\.*\.py$' -or $p -match '\\migrations\\') { exit 0 }
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$py = "$env:LOCALAPPDATA\HvacManager\venv\Scripts\python.exe"
$env:PYTHONIOENCODING = 'utf-8'
$out = & $py (Join-Path $root 'manage.py') check 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine("manage.py check failed after editing ${path}:`n$out")
    exit 2
}
exit 0
