param()

$ErrorActionPreference = "Stop"
$ruleName = "StoryForge Hub 8765 (Private LAN)"
$statusPath = Join-Path $env:APPDATA "StoryForgeStudio\firewall-status.txt"

try {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must run as administrator."
    }

    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Private
        $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort 8765
        $existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet
    }
    else {
        New-NetFirewallRule `
            -DisplayName $ruleName `
            -Description "Allow StoryForge production computers on the private local network to reach the embedded Hub." `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort 8765 `
            -Profile Private `
            -RemoteAddress LocalSubnet | Out-Null
    }

    $status = "OK $(Get-Date -Format o) - $ruleName"
    Write-Host "StoryForge Hub firewall rule is ready." -ForegroundColor Green
}
catch {
    $status = "ERROR $(Get-Date -Format o) - $($_.Exception.Message)"
    Write-Host $status -ForegroundColor Red
}

$statusDirectory = Split-Path -Parent $statusPath
New-Item -ItemType Directory -Force -Path $statusDirectory | Out-Null
Set-Content -LiteralPath $statusPath -Value $status -Encoding UTF8
Start-Sleep -Seconds 3
if ($status.StartsWith("ERROR")) { exit 1 }
