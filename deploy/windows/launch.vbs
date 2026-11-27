' Store Surveillance - hidden launcher for Windows logon.
' Starts the local API and the cloudflared tunnel with no console window.
' Drop a shortcut to this file into shell:startup to run it at logon.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)))
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run """" & root & "\deploy\windows\start_all.bat""", 0, False
