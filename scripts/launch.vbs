' Runs Bellwether.bat with no console window at all, not even a brief flash.
'
'   launch.vbs            start Bellwether if needed, then open it in a browser
'   launch.vbs /silent    start Bellwether if needed, without opening a browser
'
' Window style 0 is what keeps this invisible. Nothing here is Bellwether
' specific beyond the file name; all the logic lives in Bellwether.bat.
Dim fso, sh, root, extra
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
extra = ""
If WScript.Arguments.Count > 0 Then extra = " " & WScript.Arguments(0)
sh.Run """" & root & "\Bellwether.bat""" & extra, 0, False
