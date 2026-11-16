# One-time setup: virtualenv, .env with generated secrets, private PostgreSQL cluster, migrations.
# To use an existing PostgreSQL server instead, create .env yourself (see README) before running this.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Outside the project on purpose: compiled DLLs fail to load under long Windows paths.
$venv = "$env:LOCALAPPDATA\HvacManager\venv"
$py = "$venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv $venv }
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE) { throw 'pip install failed' }

function New-Secret { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 48 | ForEach-Object { [char]$_ }) }

if (-not (Test-Path .env)) {
    $pgPass = New-Secret
    $lines = "SECRET_KEY=$(New-Secret)$(New-Secret)", 'DEBUG=0', 'ALLOWED_HOSTS=localhost,127.0.0.1',
        'DB_NAME=hvac', 'DB_USER=hvac', "DB_PASSWORD=$pgPass", 'DB_HOST=127.0.0.1', 'DB_PORT=5433', 'LOCAL_PG=1'
    [IO.File]::WriteAllLines("$PSScriptRoot\.env", $lines)
}
$envMap = @{}
Get-Content .env | Where-Object { $_ -match '=' } | ForEach-Object { $k, $v = $_ -split '=', 2; $envMap[$k] = $v }

if ($envMap['LOCAL_PG'] -eq '1') {
    $bin = (Get-ChildItem 'C:\Program Files\PostgreSQL' | Sort-Object { [int]$_.Name } | Select-Object -Last 1).FullName + '\bin'
    if (-not (Test-Path .pgdata)) {
        $pwFile = New-TemporaryFile
        [IO.File]::WriteAllText($pwFile.FullName, $envMap['DB_PASSWORD'])
        & "$bin\initdb.exe" -D .pgdata -U hvac --pwfile=$($pwFile.FullName) --auth=scram-sha-256 --encoding=UTF8 --locale=C | Out-Null
        $initExit = $LASTEXITCODE
        Remove-Item $pwFile
        if ($initExit) { throw 'initdb failed' }
    }
    & "$bin\pg_ctl.exe" status -D .pgdata | Out-Null
    if ($LASTEXITCODE) {
        # Not piped: the server inherits handles, and a pipe would never close.
        Start-Process -FilePath "$bin\pg_ctl.exe" -ArgumentList 'start','-D','.pgdata','-w','-l','.pgdata\server.log','-o',"`"-p $($envMap['DB_PORT']) -c listen_addresses=127.0.0.1`"" -WindowStyle Hidden -PassThru | ForEach-Object { $_.WaitForExit() }
    }
    $env:PGPASSWORD = $envMap['DB_PASSWORD']
    $exists = & "$bin\psql.exe" -h 127.0.0.1 -p $envMap['DB_PORT'] -U hvac -d postgres -tAc "select 1 from pg_database where datname='hvac'"
    if (-not $exists) { & "$bin\createdb.exe" -h 127.0.0.1 -p $envMap['DB_PORT'] -U hvac hvac }
}

& $py manage.py migrate --noinput
if ($LASTEXITCODE) { throw 'migrate failed' }
& $py manage.py bootstrap
