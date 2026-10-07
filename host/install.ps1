# Registers the native messaging host with Chrome, for this user only.
#
#   powershell -ExecutionPolicy Bypass -File host\install.ps1
#   powershell -ExecutionPolicy Bypass -File host\install.ps1 -Uninstall
#
# Writes host\com.transcriber.host.json (it needs this machine's absolute
# path) and points HKCU\...\NativeMessagingHosts\com.transcriber.host at it.

param([switch]$Uninstall)

$Name = "com.transcriber.host"
# Fixed by the "key" in extension\manifest.json, so it survives reloading
# the unpacked extension. Change both together or not at all.
$ExtensionId = "cknelhogeleibhgifppgpejmdiklbknm"

$HostDir = $PSScriptRoot
$ManifestPath = Join-Path $HostDir "$Name.json"
$RegKey = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$Name"

if ($Uninstall) {
    if (Test-Path $RegKey) { Remove-Item $RegKey -Force }
    if (Test-Path $ManifestPath) { Remove-Item $ManifestPath -Force }
    Write-Host "Removed $Name."
    exit 0
}

$manifest = [ordered]@{
    name            = $Name
    description     = "Transcriber side panel host"
    path            = (Join-Path $HostDir "host.bat")
    type            = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
# UTF-8 without BOM: Chrome refuses a manifest that starts with one.
$json = $manifest | ConvertTo-Json
[System.IO.File]::WriteAllText($ManifestPath, $json, (New-Object System.Text.UTF8Encoding $false))

New-Item -Path $RegKey -Force | Out-Null
Set-ItemProperty -Path $RegKey -Name "(default)" -Value $ManifestPath

Write-Host "Registered $Name -> $ManifestPath"
Write-Host "Extension id allowed: $ExtensionId"
