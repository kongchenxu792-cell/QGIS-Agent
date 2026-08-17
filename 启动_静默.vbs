Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\桌面\QGIS-Agent"
WshShell.Run "C:\WINDOWS\system32\cmd.exe /c ""D:\桌面\QGIS-Agent\启动.bat""", 0, False
