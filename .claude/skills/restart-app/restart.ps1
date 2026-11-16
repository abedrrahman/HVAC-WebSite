# Stops the HVAC website and worker, starts run.ps1 in its own hidden window (so it outlives the caller), and waits for /login/.
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..\..')
Set-Location $root

$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'waitress|manage\.py"? worker' }
$old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }  # run.ps1 stops the worker itself once waitress exits
Write-Output "Stopped $(@($old).Count) app process(es)."

Start-Process powershell.exe -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f (Join-Path $root 'run.ps1')) -WorkingDirectory $root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(120)  # Postgres may need crash recovery first
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    try {
        $code = (Invoke-WebRequest 'http://127.0.0.1:8000/login/' -UseBasicParsing -TimeoutSec 5).StatusCode
        Write-Output "App is up: /login/ answered $code."
        exit 0
    } catch { }
}
Write-Output "App did not answer within 120 s. Check worker.err.log and .pgdata\server.log."
exit 1
