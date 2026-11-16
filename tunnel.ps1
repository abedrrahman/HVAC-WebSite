# Opens the HTTPS address for the phone app and prints it.
# With TUNNEL_TOKEN and SITE_URL in .env: the fixed Cloudflare tunnel on your own domain (the address never changes).
# Without them: a free test address (trycloudflare.com) that is new on every start and dies when the internet drops.
$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'tunnel.log'
$address = Join-Path $PSScriptRoot 'PHONE_ADDRESS.txt'
$envMap = @{}
Get-Content (Join-Path $PSScriptRoot '.env') -ErrorAction SilentlyContinue | Where-Object { $_ -match '=' -and $_ -notmatch '^\s*#' } |
    ForEach-Object { $k, $v = $_ -split '=', 2; $envMap[$k.Trim()] = $v.Trim() }
$fixed = $envMap['TUNNEL_TOKEN'] -and $envMap['SITE_URL']
$running = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue)

function Start-Cloudflared($arguments) {
    # cloudflared.exe was downloaded from inside the Claude app, which keeps its own copy of AppData\Local.
    $exe = @("$env:LOCALAPPDATA\HvacManager\cloudflared.exe") + @((Get-Item "$env:LOCALAPPDATA\Packages\Claude_*\LocalCache\Local\HvacManager\cloudflared.exe" -ErrorAction SilentlyContinue).FullName) |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $exe) { throw 'cloudflared.exe not found in AppData\Local\HvacManager.' }
    Remove-Item $log -ErrorAction SilentlyContinue
    Start-Process -FilePath $exe -ArgumentList ($arguments + @('--logfile', ('"{0}"' -f $log))) -WindowStyle Hidden
}

if ($fixed) {
    # A test tunnel from before the switch would answer on the wrong address: stop it.
    $running | Where-Object { $_.CommandLine -match '--url' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    if (-not ($running | Where-Object { $_.CommandLine -notmatch '--url' })) {
        $env:TUNNEL_TOKEN = $envMap['TUNNEL_TOKEN']  # passed through the environment, so the secret never shows in the process list
        Start-Cloudflared @('tunnel', 'run')
        Remove-Item Env:TUNNEL_TOKEN
    }
    $url = $envMap['SITE_URL'].TrimEnd('/')
    Set-Content $address $url -Encoding ascii
    Write-Output "Phone app address: $url (fixed, it does not change)"
    exit 0
}

# Test address. It dies when the PC sleeps or loses the internet, while cloudflared keeps running: stop it so a new address is made.
if ($running -and (Test-Path $address)) {
    $name = ([uri](Get-Content $address -TotalCount 1)).Host
    if (-not (Resolve-DnsName $name -Server 1.1.1.1 -Type A -ErrorAction SilentlyContinue)) {
        Write-Output 'The old phone address no longer works. Making a new one...'
        $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Start-Sleep 2
        $running = @()
    }
}
if (-not $running) {
    Start-Cloudflared @('tunnel', '--url', 'http://127.0.0.1:8000')
}
foreach ($i in 1..30) {
    $found = if (Test-Path $log) { Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -Last 1 }
    if ($found) {
        $url = $found.Matches[0].Value
        Set-Content $address $url -Encoding ascii
        Write-Output "Phone app address: $url"
        exit 0
    }
    Start-Sleep 1
}
Write-Output 'The tunnel did not give an address within 30 seconds. Check the internet connection and tunnel.log.'
exit 1
