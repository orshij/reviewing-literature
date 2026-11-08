-- Litrev launcher. install.sh compiles this into ~/Applications/Litrev.app, putting this line first:
--     property litrevApp : "<absolute path of litrev-app>"
-- `litrev-app open` starts the app's local server if it is not running and opens it in the browser.
-- The command runs in the background with its output discarded, so the applet returns at once
-- (do shell script waits until the command's output is closed).
on run
	try
		do shell script "test -x " & quoted form of litrevApp
	on error
		display dialog "Litrev is not installed at " & litrevApp & "." & return & return & "Run install.sh again." buttons {"OK"} default button 1 with icon caution
		return
	end try
	do shell script "nohup " & quoted form of litrevApp & " open > /dev/null 2>&1 &"
end run
