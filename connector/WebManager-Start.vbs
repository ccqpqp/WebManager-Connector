Option Explicit
Dim shell, fso, root, command, mode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
mode = ""
If WScript.Arguments.Count > 0 Then
  If LCase(WScript.Arguments(0)) = "background" Then mode = " -Background"
End If
command = "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & root & "\WebManager-Tray.ps1" & Chr(34) & mode
shell.Run command, 0, False
