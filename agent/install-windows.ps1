#Requires -RunAsAdministrator
# Agent installer for Windows (ASCII-only on purpose: safe for any file encoding).
# Run from the agent folder, as Administrator, under the user who works at this PC:
#   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Secret <SECRET> -ConfigureWol
# -Secret       secret issued by the server command "rcctl add" (omit to generate a new one)
# -ConfigureWol enable Wake-on-LAN, disable Fast Startup and hibernation
# The agent is installed as a "run at user logon" scheduled task (not a service),
# so it runs in the user session: screenshots and screen lock work.
# Safe to run again: it updates files and restarts the agent.
param([int]$Port = 8765, [switch]$ConfigureWol, [string]$Secret = "")
$ErrorActionPreference = "Stop"
$src  = $PSScriptRoot
$dir  = Join-Path $env:LOCALAPPDATA "rc-agent"
$task = "RC-Agent"

if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = "py"; $pyArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = "python"; $pyArgs = @() }
else { throw "Python 3.10+ not found. Install from python.org (tick 'Add to PATH')." }
if ($Secret -ne "" -and $Secret.Length -lt 32) { throw "Secret must be at least 32 characters." }

# Stop a running agent first (a running venv python blocks updates)
Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item "$src\agent.py","$src\signing.py","$src\requirements.txt" $dir -Force
if (-not (Test-Path "$dir\config.yaml")) {
    if (Test-Path "$src\config.yaml") { Copy-Item "$src\config.yaml" "$dir\config.yaml" }
    else { Copy-Item "$src\config.example.yaml" "$dir\config.yaml" }
}

& $pyExe @pyArgs -m venv "$dir\venv"
& "$dir\venv\Scripts\python.exe" -m pip install --upgrade pip -q
& "$dir\venv\Scripts\python.exe" -m pip install -r "$dir\requirements.txt" -q

# Secret
if ($Secret -ne "") {
    "AGENT_SECRET=$Secret" | Set-Content "$dir\.env" -Encoding ascii
    Write-Host "AGENT_SECRET set from the -Secret parameter."
} elseif (-not (Test-Path "$dir\.env")) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $gen = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    "AGENT_SECRET=$gen" | Set-Content "$dir\.env" -Encoding ascii
    Write-Host ""
    Write-Host "Generated AGENT_SECRET: $gen" -ForegroundColor Yellow
    Write-Host "Register this device on the server: rcctl add NAME --address <tailscale-ip> --mac <MAC> --secret $gen"
    Write-Host ""
}
# Folder permissions: current user, SYSTEM and Administrators only
icacls $dir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

# Firewall: agent port only from the Tailscale range
Get-NetFirewallRule -DisplayName "RC-Agent" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "RC-Agent" -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any | Out-Null

# Scheduled task: at logon, auto-restart
Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
$user    = "$env:USERDOMAIN\$env:USERNAME"
$action  = New-ScheduledTaskAction -Execute "$dir\venv\Scripts\pythonw.exe" -Argument "agent.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
           -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
$prin    = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $set -Principal $prin | Out-Null
Start-ScheduledTask -TaskName $task
Write-Host "Agent installed to $dir and started (port $Port, Tailscale only)." -ForegroundColor Green

if ($ConfigureWol) {
    # Fast Startup and hibernation break WoL and turn "sleep" into hibernation
    powercfg /hibernate off
    Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name HiberbootEnabled -Value 0
    foreach ($nic in Get-NetAdapter -Physical | Where-Object Status -eq "Up") {
        try {
            Set-NetAdapterPowerManagement -Name $nic.Name -WakeOnMagicPacket Enabled -WakeOnPattern Disabled -NoRestart
            Write-Host "WoL (magic packet) enabled: $($nic.Name)  MAC: $($nic.MacAddress)"
        }
        catch { Write-Warning "Failed for $($nic.Name): enable it manually in Device Manager" }
    }
    Write-Host "Also check WoL in BIOS/UEFI and the adapter properties: 'Wake on Magic Packet' = Enabled."
}
