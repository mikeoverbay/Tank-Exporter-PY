@echo off
:: Standalone icon test -- opens a tiny pygame window with the TEPY
:: tepee set as its icon.  If you see the tepee in the title bar /
:: taskbar / alt-tab card of the window this opens, the icon files
:: are good and any "icon missing" symptom in the main app is a
:: viewer-side bug to chase from there.
::
:: See cust_tools/test_icon.py for what it actually does.

cd /d "%~dp0"
python cust_tools\test_icon.py
pause
