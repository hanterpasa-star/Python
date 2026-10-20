param([switch]$Remove, [switch]$RegisterOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$digest = [System.Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))
$suffix = ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 8)
$taskName = "ShoeShopBot-$suffix"
if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Host 'Automatic startup disabled for this folder.'
    exit 0
}
$python = Join-Path $root '.venv\Scripts\python.exe'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (!(Test-Path $pythonw) -or !(Test-Path (Join-Path $root '.env'))) {
    throw 'First run START_BOT.bat, configure the bot, then stop it with Ctrl+C.'
}
Push-Location $root
try {
    & $python -c 'from shoe_shop_bot.config import Settings; s=Settings.load(); assert s.owner_id, "Set OWNER_ID first"'
    if ($LASTEXITCODE -ne 0) { throw 'Fix the settings in EDIT_SETTINGS.bat first.' }
} finally { Pop-Location }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m shoe_shop_bot.supervisor' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output "TaskName=$taskName"
if (!$RegisterOnly) { Start-ScheduledTask -TaskName $taskName }
Write-Host 'Automatic startup enabled. The bot starts now and at your next Windows sign-in.'
Write-Host 'Keep the laptop powered on, awake and connected to the internet.'
