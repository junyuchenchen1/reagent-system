Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell -NoProfile -Command ""Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }""", 0, True
MsgBox "·þÎñÒÑÍ£Ö¹¡£", vbInformation