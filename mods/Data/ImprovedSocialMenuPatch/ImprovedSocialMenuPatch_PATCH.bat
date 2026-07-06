@echo off
IF NOT EXIST "..\SeventySix - Interface.ba2" (
	echo Error: SeventySix - Interface.ba2 not found!
	echo Make sure you are running script in the correct directory: Fallout 76/Data/ImprovedSocialMenuPatch/
	echo.
	pause
	exit
)
@echo on

copy "..\SeventySix - Interface.ba2" "SeventySix - Interface.ba2.bak"

Archive2\archive2.exe "..\SeventySix - Interface.ba2" -quiet -extract="Archive2\data"

copy /Y "interface\overlay.swf" "Archive2\data\interface\overlay.swf"

Archive2\archive2.exe Archive2\data\ -quiet -compression=None -create="SeventySix - Interface.ba2"

copy /Y "SeventySix - Interface.ba2" "..\SeventySix - Interface.ba2"

rmdir /S /Q Archive2\data

del /Q "SeventySix - Interface.ba2"

pause