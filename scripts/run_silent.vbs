Set objArgs = WScript.Arguments
If objArgs.Count < 3 Then WScript.Quit

strExe = objArgs(0)
strDir = objArgs(1)
strScript = objArgs(2)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = strDir

Set objEnv = WshShell.Environment("PROCESS")
objEnv("PYTHONPATH") = strDir

strCmd = """" & strExe & """ """ & strScript & """"
WshShell.Run strCmd, 0, False
Set WshShell = Nothing
