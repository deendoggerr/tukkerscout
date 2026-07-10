@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$desktop=[Environment]::GetFolderPath('Desktop');" ^
 "$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut($desktop+'\TukkerScout.lnk');" ^
 "$shortcut.TargetPath='%~dp0start_tukkerscout.bat';" ^
 "$shortcut.WorkingDirectory='%~dp0';" ^
 "$shortcut.Description='Start TukkerScout';" ^
 "$shortcut.Save()"
echo De snelkoppeling TukkerScout staat nu op je bureaublad.
pause
