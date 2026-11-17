@echo off
rem Builds dist\CalendarViewer.exe
cd /d "%~dp0"
py -m pip install --upgrade caldav icalendar recurring-ical-events keyring tzdata pyinstaller pillow || goto :err
py make_icon.py || goto :err
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name CalendarViewer ^
  --icon app.ico --add-data "app.ico;." ^
  --collect-all caldav --collect-all icalendar --collect-all recurring_ical_events ^
  --collect-all x_wr_timezone --collect-data tzdata ^
  --collect-submodules keyring --hidden-import win32ctypes.core ^
  calendar_app.py || goto :err
echo.
echo Done: %~dp0dist\CalendarViewer.exe
goto :eof
:err
echo Build failed.
exit /b 1
