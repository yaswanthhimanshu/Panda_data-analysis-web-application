
' Windows launcher for the Panda desktop application.
' User workflow: double-click this file to start desktop.py without a console.
' System workflow: resolve the project folder, use its virtual-environment
' pythonw.exe, and launch the pywebview/Flask desktop process in the background.

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
 
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = appDir & "\.venv\Scripts\pythonw.exe"
desktopPath = appDir & "\desktop.py"
 
shell.CurrentDirectory = appDir
shell.Run """" & pythonwPath & """ """ & desktopPath & """", 0, False
