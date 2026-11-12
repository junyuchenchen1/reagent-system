Option Explicit
Dim fso, shell, baseDir, cmd, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell -NoProfile -Command ""$c = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 1 } else { exit 0 }"""
code = shell.Run(cmd, 0, True)
If code = 0 Then
    shell.CurrentDirectory = baseDir
    shell.Run "cmd /c python run.py >> """ & baseDir & "\logs\server.log"" 2>&1", 0, False
End If