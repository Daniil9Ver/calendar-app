# Calendar Viewer for Windows

A small Windows app that shows your calendars in **Day / Week / Month** views. Open it, look at your events, close it.

It works with **any calendar subscription link** (`.ics` / iCal / `webcal://`): Google Calendar, Outlook,
university and school timetables, sports fixtures, public holidays and so on. You can add as many links as you like.
It can also sign in to **iCloud** directly to show your Apple calendars.

## Use it

1. Run `dist\CalendarViewer.exe`. It's a single file and runs on any 64-bit Windows 10/11 PC.
   You don't need to install Python or anything else. Copy it to a USB stick or another PC and double-click it.
2. The first time, the **Calendars** window opens. Add one or both:
   - **Calendar feed links (iCal / .ics):** any calendar you can subscribe to. `webcal://`, `https://` and `http://` links all work.
     For example:
     - **Google Calendar:** Settings → your calendar → Integrate calendar → *Secret address in iCal format*.
     - **Outlook / Microsoft 365:** Settings → Calendar → Shared calendars → *Publish a calendar* → ICS link.
     - **University timetable:** e.g. in Calendar Centre, click *Save & Generate URL → Copy Link*.
     - **iPhone subscribed calendars:** Settings → Calendar → Accounts → Subscribed Calendars.
   - **iCloud account (optional):** Apple ID plus an *app-specific* password (not your normal Apple password).
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

To try it without adding any calendars: `CalendarViewer.exe --demo`

## Controls

| Action | How |
|---|---|
| Previous / next | `‹` `›` buttons, ← → keys, or mouse wheel (month view) |
| Jump to today | **Today** or `T` |
| Switch view | Day / Week / Month buttons, or `D` `W` `M` |
| Open a day | Click a day in month view, or a day header in week view |
| Event details | Click the event |
| Reload calendars | **Refresh** or `F5` (also refreshes automatically every 15 min) |

Read-only: it shows events but doesn't create or edit them.

## Rebuild the exe

Needs Python 3 (`py` launcher). Run `build.bat`. Output: `dist\CalendarViewer.exe`.
Run from source: `py calendar_app.py`.

Settings are at the top of `calendar_app.py`: `FIRST_WEEKDAY` (0 = Monday, 6 = Sunday), colours, auto-refresh interval.
