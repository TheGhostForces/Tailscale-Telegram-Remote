#Requires -RunAsAdministrator
# Removes the agent from this PC (ASCII-only on purpose).
#   powershell -ExecutionPolicy Bypass -File .\uninstall-windows.ps1
$dir = Join-Path $env:LOCALAPPDATA "rc-agent"
Stop-ScheduledTask -TaskName "RC-Agent" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "RC-Agent" -Confirm:$false -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName "RC-Agent" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Agent removed from this PC."
Write-Host "Next: remove the device on the server (rcctl remove NAME) and in Tailscale admin console (Machines -> Remove)."
Write-Host "Optional: re-enable hibernation with 'powercfg /hibernate on' if you want it back."
