# iCloud Calendar for Windows

A small Windows app that shows your iCloud calendars in **Day / Week / Month** views. Open it, look at your events, close it.

## Use it

1. Run `dist\iCloudCalendar.exe`. It's a single file and runs on any 64-bit Windows 10/11 PC.
   You don't need to install Python or anything else. Copy it to a USB stick or another PC and double-click it.
2. The first time, the **Calendars** window opens. Add one or both:
   - **Calendar feed links (iCal):** e.g. your university timetable. In Calendar Centre, click
     *Save & Generate URL → Copy Link* and paste it in. `webcal://` and `https://` links both work.
     On an iPhone you can also find the link under Settings → Calendar → Accounts → Subscribed Calendars.
   - **iCloud account:** Apple ID plus an *app-specific* password (not your normal Apple password).
     Create one at https://account.apple.com → Sign-In and Security → App-Specific Passwords.
     Note: calendars you *subscribe to* on the iPhone are not visible through iCloud. Add their link as a feed instead.
3. Everything is saved **next to the exe**, so the folder is portable:
   - `calendar_settings.json`: Apple ID, app password and feed links, stored **unencrypted**.
     Keep this file private, and don't share or upload the folder.
   - `calendar_log.txt`: diagnostic log (no passwords or links).

   Copy the whole folder (exe + settings) to a USB stick or another PC and it works with no setup.
   If you copy only the exe, it starts empty and asks for calendars.
   **Calendars → Remove all** deletes the settings file.
   If the exe's folder isn't writable (e.g. Program Files), data goes to `%LOCALAPPDATA%\iCloudCalendar` instead.

To try it without signing in: `iCloudCalendar.exe --demo`

## Controls

| Action | How |
|---|---|
| Previous / next | `‹` `›` buttons, ← → keys, or mouse wheel (month view) |
| Jump to today | **Today** or `T` |
| Switch view | Day / Week / Month buttons, or `D` `W` `M` |
| Open a day | Click a day in month view, or a day header in week view |
| Event details | Click the event |
| Reload from iCloud | **Refresh** or `F5` (also refreshes automatically every 15 min) |

Read-only: it shows events but doesn't create or edit them.

## Rebuild the exe

Needs Python 3 (`py` launcher). Run `build.bat`. Output: `dist\iCloudCalendar.exe`.
Run from source: `py calendar_app.py`.

Settings at the top of `calendar_app.py`: `FIRST_WEEKDAY` (0 = Monday, 6 = Sunday), colours, auto-refresh interval.
