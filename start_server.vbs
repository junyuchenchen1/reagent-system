Option Explicit
Dim fso, shell, baseDir, logDir, cmd, code, silent, ip
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
silent = False
If WScript.Arguments.Count > 0 Then
    If WScript.Arguments(0) = "silent" Then silent = True
End If
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = baseDir & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
cmd = "powershell -NoProfile -Command ""$c = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 1 } else { exit 0 }"""
code = shell.Run(cmd, 0, True)
If code = 1 Then
    If Not silent Then MsgBox "服务已在端口 5000 运行中。", vbInformation
    WScript.Quit
End If
shell.CurrentDirectory = baseDir
cmd = "cmd /c python run.py >> """ & logDir & "\server.log"" 2>&1"
shell.Run cmd, 0, False
WScript.Sleep 2000
On Error Resume Next
Dim exec, output, lines, i, parts
Set exec = shell.Exec("ipconfig")
output = exec.StdOut.ReadAll
ip = ""
lines = Split(output, vbCrLf)
For i = 0 To UBound(lines)
    If InStr(lines(i), "IPv4") > 0 Then
        parts = Split(lines(i), ":")
        If UBound(parts) >= 1 Then
            ip = Trim(parts(1))
            If Left(ip, 3) <> "127" Then Exit For
        End If
    End If
Next
If ip = "" Then ip = "127.0.0.1"
If Not silent Then MsgBox "检验科试剂管理系统已启动。" & vbCrLf & vbCrLf & "本机访问:  http://127.0.0.1:5000" & vbCrLf & "局域网访问: http://" & ip & ":5000", vbInformation