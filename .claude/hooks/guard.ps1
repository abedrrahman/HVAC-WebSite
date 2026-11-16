# PreToolUse (Edit|Write): refuse edits to secrets and live data. Exit 2 blocks the tool call and tells Claude why.
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
$path = [string]$data.tool_input.file_path
if (-not $path) { exit 0 }
$p = $path.Replace('/', '\')
if ($p -match '(^|\\)\.env$|(^|\\)\.pgdata\\|(^|\\)backups\\|(^|\\)FIRST_LOGIN\.txt$') {
    [Console]::Error.WriteLine("Blocked: $path holds secrets or live data (.env, .pgdata, backups, FIRST_LOGIN.txt). Ask the user to change it by hand.")
    exit 2
}
exit 0
