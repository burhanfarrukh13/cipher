$WshShell = New-Object -ComObject WScript.Shell
$shortcut = $WshShell.CreateShortcut("$PSScriptRoot\Cipher.lnk")
$shortcut.TargetPath = "$PSScriptRoot\Cipher.bat"
$shortcut.WorkingDirectory = "$PSScriptRoot"
$shortcut.IconLocation = "$PSScriptRoot\favicon.ico"
$shortcut.Save()
Write-Host "Created Cipher.lnk with the favicon icon - use that to launch from now on."
