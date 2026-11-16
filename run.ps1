# Starts PostgreSQL (if private), the reminder worker and the web server on http://localhost:8000
# Set LISTEN=0.0.0.0:8000 in .env (and add the host to ALLOWED_HOSTS) to reach it from phones for QR scanning.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = "$env:LOCALAPPDATA\HvacManager\venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    # The environment was created from inside the Claude app, which keeps its own copy of AppData\Local. Windows itself (the sign-in task) has to look there.
    $py = (Get-Item "$env:LOCALAPPDATA\Packages\Claude_*\LocalCache\Local\HvacManager\venv\Scripts\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
    if (-not $py) { throw 'Python environment not found in AppData\Local\HvacManager\venv.' }
}
$envMap = @{}
Get-Content .env | Where-Object { $_ -match '=' } | ForEach-Object { $k, $v = $_ -split '=', 2; $envMap[$k] = $v }

if ($envMap['LOCAL_PG'] -eq '1') {
    $bin = (Get-ChildItem 'C:\Program Files\PostgreSQL' | Sort-Object { [int]$_.Name } | Select-Object -Last 1).FullName + '\bin'
    & "$bin\pg_ctl.exe" status -D .pgdata | Out-Null
    if ($LASTEXITCODE) {
        Start-Process -FilePath "$bin\pg_ctl.exe" -ArgumentList 'start','-D','.pgdata','-w','-l','.pgdata\server.log','-o',"`"-p $($envMap['DB_PORT']) -c listen_addresses=127.0.0.1`"" -WindowStyle Hidden -PassThru | ForEach-Object { $_.WaitForExit() }
    }
}
& $py manage.py migrate --noinput
$listen = if ($envMap['LISTEN']) { $envMap['LISTEN'] } else { '127.0.0.1:8000' }
$worker = Start-Process $py -ArgumentList 'manage.py', 'worker' -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput worker.log -RedirectStandardError worker.err.log
try {
    & $py -m waitress --listen=$listen --threads=8 hvac.wsgi:application
} finally {
    Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue
}
