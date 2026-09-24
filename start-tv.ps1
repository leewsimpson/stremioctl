# Start the Stremio MCP server and the Cloudflare tunnel that exposes it.
# Run at logon (Task Scheduler) or by hand. The MCP server must run in your
# desktop session because it launches the Stremio window.
#   .\start-tv.ps1            start both in the background
#   .\start-tv.ps1 -Url       print the connector URL to paste into ChatGPT or Claude
param([switch]$Url)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$envVars = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Za-z_]+)\s*=\s*(.*)$' } |
    ForEach-Object { $envVars[$Matches[1]] = $Matches[2].Trim('"', "'") }
$port = if ($envVars.MCP_PORT) { $envVars.MCP_PORT } else { '8766' }
$tunnel = if ($envVars.TUNNEL_NAME) { $envVars.TUNNEL_NAME } else { 'stremio-tv' }

if ($Url) {
    "https://$($envVars.MCP_PUBLIC_HOST)/mcp/$($envVars.MCP_SECRET)"
    return
}

$logs = Join-Path $env:LOCALAPPDATA 'stremioctl'
New-Item -ItemType Directory -Force $logs | Out-Null

if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -WindowStyle Hidden "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList 'stremio_mcp.py' `
        -RedirectStandardError "$logs\mcp.log" -RedirectStandardOutput "$logs\mcp.out.log"
    "MCP server started on port $port"
} else { "MCP server already listening on port $port" }

# Its own config file, because a default ~/.cloudflared/config.yml may pin another tunnel.
$config = Join-Path $HOME ".cloudflared\$tunnel.yml"
$running = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
    Where-Object { $_.CommandLine -like "*$tunnel.yml*" }
if (-not $running) {
    Start-Process -WindowStyle Hidden cloudflared -ArgumentList '--config', $config, '--no-autoupdate', 'tunnel', 'run' `
        -RedirectStandardError "$logs\tunnel.log" -RedirectStandardOutput "$logs\tunnel.out.log"
    "Tunnel '$tunnel' started -> https://$($envVars.MCP_PUBLIC_HOST)"
} else { "Tunnel '$tunnel' already running" }
"Logs: $logs"
