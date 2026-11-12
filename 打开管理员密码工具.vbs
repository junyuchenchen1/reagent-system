Option Explicit
Dim fso, shell, baseDir, commandLine, toolPath, file
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
toolPath = ""

' Keep this launcher ASCII-only: Windows Script Host may not parse UTF-8 VBS files.
For Each file In fso.GetFolder(baseDir).Files
    If LCase(fso.GetExtensionName(file.Name)) = "pyw" Then
        toolPath = file.Path
        Exit For
    End If
Next

If toolPath = "" Then
    MsgBox "未找到管理员密码工具.pyw", vbExclamation, "启动失败"
    WScript.Quit 1
End If

shell.CurrentDirectory = baseDir
commandLine = "python """ & toolPath & """"
On Error Resume Next
shell.Run commandLine, 0, False
If Err.Number <> 0 Then
    MsgBox "无法启动密码工具。请确认已安装 Python。.", vbExclamation, "启动失败"
End If
