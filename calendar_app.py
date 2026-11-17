"""
Calendar Viewer for Windows
---------------------------
A small desktop calendar (Day / Week / Month) that shows any subscribed calendar
(.ics / iCal / webcal links from Google, Outlook, university timetables, etc.)
and, optionally, your iCloud calendars over CalDAV. Settings and log are kept next to the .exe.

Run:  py calendar_app.py          (your calendars)
      py calendar_app.py --demo   (sample events, no sign-in)
"""

from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

APP_NAME = "Calendar Viewer"
KEYRING_SERVICE = "iCloudCalendarApp"
KEYRING_USER_KEY = "__apple_id__"
KEYRING_FEEDS_KEY = "__ics_feeds__"
CALDAV_URL = "https://caldav.icloud.com"
APP_PASSWORD_URL = "https://account.apple.com/account/manage"
FIRST_WEEKDAY = 0  # 0 = Monday, 6 = Sunday
AUTO_REFRESH_MS = 15 * 60 * 1000
# Old internal names kept on purpose so existing settings still load.
LEGACY_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "iCloudCalendar")


def _pick_data_dir() -> str:
    """Keep data next to the .exe (portable). Fall back to %LOCALAPPDATA% if that folder is read-only."""
    here = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    try:
        probe = os.path.join(here, ".write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return here
    except OSError:
        os.makedirs(LEGACY_DIR, exist_ok=True)
        return LEGACY_DIR


DATA_DIR = _pick_data_dir()
SETTINGS_FILE = os.path.join(DATA_DIR, "calendar_settings.json")
LOG_PATH = os.path.join(DATA_DIR, "calendar_log.txt")


def read_settings():
    """Return the saved settings dict, or None if there is no settings file yet."""
    import json
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except Exception as ex:
        log(f"could not read {SETTINGS_FILE}: {ex!r}")
        return {}


def write_settings(apple_id: str, app_password: str, feeds: list[str]):
    import json
    if not apple_id and not feeds:
        if os.path.exists(SETTINGS_FILE):
            os.remove(SETTINGS_FILE)
        return
    data = {
        "_note": "Calendar Viewer settings. Contains private links and your app password - keep this file private.",
        "apple_id": apple_id,
        "app_password": app_password,
        "feeds": feeds,
    }
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)


def log(msg: str):
    """Append a line to the diagnostic log (never contains passwords)."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTHS_SHORT = [m[:3] for m in MONTHS]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_SHORT = [d[:3] for d in WEEKDAYS]

C = {
    "bg": "#ffffff",
    "toolbar": "#f6f7f9",
    "grid": "#e4e6ea",
    "grid_soft": "#f0f1f3",
    "text": "#1f2328",
    "muted": "#868d97",
    "accent": "#0a84ff",
    "today_bg": "#f2f7ff",
    "out_month": "#fafbfc",
    "now": "#ff3b30",
}
PALETTE = ["#0a84ff", "#ff9f0a", "#30d158", "#bf5af2", "#ff375f", "#5ac8fa", "#ffcc00", "#a2845e"]


# --------------------------------------------------------------------------- model

@dataclass
class Event:
    title: str
    start: dt.datetime          # local, naive
    end: dt.datetime            # local, naive, exclusive
    all_day: bool
    calendar: str
    color: str
    location: str = ""
    notes: str = ""
    uid: str = field(default="")

    def key(self):
        return (self.uid, self.start, self.calendar, self.title)


def _to_local(value):
    """Return (naive local datetime, is_date)."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    return dt.datetime.combine(value, dt.time.min), True


def _norm_color(value):
    if isinstance(value, str) and value.startswith("#") and len(value) >= 7:
        return value[:7]
    return None


def event_from_component(comp, cal_name, color) -> Event | None:
    dtstart = comp.get("DTSTART")
    if dtstart is None:
        return None
    start_raw = dtstart.dt
    if comp.get("DTEND") is not None:
        end_raw = comp.get("DTEND").dt
    elif comp.get("DURATION") is not None:
        end_raw = start_raw + comp.get("DURATION").dt
    elif isinstance(start_raw, dt.datetime):
        end_raw = start_raw
    else:
        end_raw = start_raw + dt.timedelta(days=1)
    start, all_day = _to_local(start_raw)
    end, _ = _to_local(end_raw)
    if end < start:
        end = start
    return Event(
        title=str(comp.get("SUMMARY", "")).strip() or "(No title)",
        start=start, end=end, all_day=all_day,
        calendar=cal_name, color=color,
        location=str(comp.get("LOCATION", "") or "").strip(),
        notes=str(comp.get("DESCRIPTION", "") or "").strip(),
        uid=str(comp.get("UID", "")),
    )


# --------------------------------------------------------------------------- sources

class ICloudSource:
    """Reads events from iCloud over CalDAV."""

    def __init__(self, apple_id: str, password: str):
        self.apple_id = apple_id
        self.password = password
        self.label = apple_id
        self._calendars = None

    def _connect(self):
        import caldav
        try:
            client = caldav.DAVClient(url=CALDAV_URL, username=self.apple_id,
                                      password=self.password, features="icloud", timeout=30)
        except Exception:
            client = caldav.DAVClient(url=CALDAV_URL, username=self.apple_id,
                                      password=self.password, timeout=30)
        log(f"connecting to {CALDAV_URL} as {self.apple_id}")
        principal = client.principal()
        log(f"principal: {principal.url}")
        all_cals = principal.calendars()
        log(f"server returned {len(all_cals)} calendar collection(s)")
        cals = []
        for cal in all_cals:
            try:
                comps = cal.get_supported_components()
            except Exception as ex:
                comps = None
                log(f"  {cal.url}: supported components unknown ({ex})")
            log(f"  calendar '{cal.name}' components={comps} url={cal.url}")
            if comps and "VEVENT" not in comps:
                continue  # reminders lists etc.
            color = None
            try:
                from caldav.elements import ical as cdical
                color = _norm_color(cal.get_property(cdical.CalendarColor()))
            except Exception:
                pass
            name = cal.name or "Calendar"
            cals.append((cal, name, color or PALETTE[len(cals) % len(PALETTE)]))
        self._calendars = cals

    def fetch(self, start: dt.datetime, end: dt.datetime):
        import icalendar
        import recurring_ical_events

        if self._calendars is None:
            self._connect()
        s, e = start.astimezone(), end.astimezone()
        log(f"fetch {s:%Y-%m-%d} .. {e:%Y-%m-%d} from {len(self._calendars)} event calendar(s)")
        events, warnings = [], []
        for cal, name, color in self._calendars:
            try:
                results = cal.search(start=s, end=e, event=True, expand=False)
            except Exception as ex:
                log(f"  '{name}': search failed: {ex!r}\n{traceback.format_exc()}")
                warnings.append(f"{name}: {ex}")
                continue
            if not results:
                # Some servers answer a time-range query with nothing; load everything and filter here.
                try:
                    results = cal.events()
                    log(f"  '{name}': date search empty; full calendar has {len(results)} object(s)")
                except Exception as ex:
                    log(f"  '{name}': loading all events failed: {ex!r}")
            before = len(events)
            for obj in results:
                try:
                    ical = icalendar.Calendar.from_ical(obj.data)
                    for comp in recurring_ical_events.of(ical).between(s, e):
                        ev = event_from_component(comp, name, color)
                        if ev:
                            events.append(ev)
                except Exception as ex:  # one broken event shouldn't hide the rest
                    log(f"  '{name}': could not read one event: {ex!r}")
                    warnings.append(f"{name}: {ex}")
            log(f"  '{name}': {len(results)} object(s) -> {len(events) - before} event(s)")
        return events, warnings


def normalize_feed_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


_BAD_DURATION = None


def fix_ics(data: bytes) -> str:
    """Repair common mistakes in real-world feeds before strict parsing.

    E.g. some university timetable feeds write durations as 'P1H' instead of 'PT1H'.
    """
    import re
    global _BAD_DURATION
    if _BAD_DURATION is None:
        _BAD_DURATION = re.compile(r"(?m)^((?:DURATION|TRIGGER|REFRESH-INTERVAL|X-PUBLISH-TTL)[^:\r\n]*:-?)P((?:\d+[HMS])+)(\r?)$")
    text = data.decode("utf-8", errors="replace")
    return _BAD_DURATION.sub(r"\1PT\2\3", text)


class IcsFeedSource:
    """Reads a subscribed iCal (.ics) feed, e.g. a university timetable link."""

    CACHE_SECONDS = 10 * 60

    def __init__(self, url: str, color: str):
        self.url = normalize_feed_url(url)
        self.color = color
        from urllib.parse import urlparse
        self.name = urlparse(self.url).hostname or "Feed"
        self._cal = None
        self._loaded_at = None

    def reset(self):
        self._cal = None

    def _load(self):
        import icalendar
        import urllib.request
        fresh = self._loaded_at and (dt.datetime.now() - self._loaded_at).total_seconds() < self.CACHE_SECONDS
        if self._cal is not None and fresh:
            return self._cal
        req = urllib.request.Request(self.url, headers={"User-Agent": "CalendarViewer/1.0 (Windows)",
                                                        "Accept": "text/calendar, */*"})
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = resp.read()
        self._cal = icalendar.Calendar.from_ical(fix_ics(data))
        self._loaded_at = dt.datetime.now()
        name = self._cal.get("X-WR-CALNAME")
        if name and str(name).strip():
            self.name = str(name).strip()
        if "@" in self.name and "google.com" in self.url:
            self.name = "Google"  # primary Google calendars are named after the email address
        from urllib.parse import urlparse
        log(f"feed '{self.name}' ({urlparse(self.url).hostname}): downloaded {len(data)} bytes, "
            f"{len(self._cal.walk('VEVENT'))} VEVENT(s)")
        return self._cal

    def fetch(self, start: dt.datetime, end: dt.datetime):
        import recurring_ical_events
        cal = self._load()
        s, e = start.astimezone(), end.astimezone()
        events = []
        for comp in recurring_ical_events.of(cal).between(s, e):
            ev = event_from_component(comp, self.name, self.color)
            if ev:
                events.append(ev)
        log(f"feed '{self.name}': {len(events)} event(s) in {s:%Y-%m-%d} .. {e:%Y-%m-%d}")
        return events, []


class CombinedSource:
    """iCloud account (optional) + any number of iCal feed links."""

    def __init__(self, icloud: "ICloudSource | None", feed_urls: list[str]):
        self.icloud = icloud
        self.feeds = [IcsFeedSource(u, PALETTE[(i + 2) % len(PALETTE)]) for i, u in enumerate(feed_urls)]

    @property
    def label(self):
        parts = []
        if self.icloud:
            parts.append(self.icloud.apple_id)
        if self.feeds:
            parts.append(f"{len(self.feeds)} feed(s)")
        return ", ".join(parts)

    @property
    def calendar_count(self):
        n = len(self.feeds)
        if self.icloud and self.icloud._calendars:
            n += len(self.icloud._calendars)
        return n

    def reset(self):
        if self.icloud:
            self.icloud._calendars = None
        for f in self.feeds:
            f.reset()

    def fetch(self, start, end):
        events, warnings = [], []
        if self.icloud:
            try:
                ev, w = self.icloud.fetch(start, end)
                events += ev
                warnings += w
            except Exception as ex:
                if not self.feeds:
                    raise
                log(f"iCloud failed: {ex!r}\n{traceback.format_exc()}")
                warnings.append(f"iCloud: {ex}")
        for feed in self.feeds:
            try:
                ev, w = feed.fetch(start, end)
                events += ev
                warnings += w
            except Exception as ex:
                log(f"feed '{feed.name}' failed: {ex!r}\n{traceback.format_exc()}")
                if not self.icloud and len(self.feeds) == 1:
                    raise
                warnings.append(f"{feed.name}: {ex}")
        return events, warnings


class DemoSource:
    label = "demo data"
    calendar_count = 3

    def reset(self):
        pass

    def fetch(self, start: dt.datetime, end: dt.datetime):
        work, personal, family = ("Work", "#0a84ff"), ("Personal", "#30d158"), ("Family", "#ff9f0a")
        today = dt.date.today()
        out = []

        def add(title, d, h1, m1, h2, m2, cal, loc=""):
            out.append(Event(title, dt.datetime.combine(d, dt.time(h1, m1)),
                             dt.datetime.combine(d, dt.time(h2, m2)), False, cal[0], cal[1], loc))

        def add_all_day(title, d1, days, cal):
            s = dt.datetime.combine(d1, dt.time.min)
            out.append(Event(title, s, s + dt.timedelta(days=days), True, cal[0], cal[1]))

        d = start.date()
        while d < end.date():
            off = (d - today).days
            if d.weekday() < 5:
                add("Standup", d, 9, 30, 9, 45, work, "Teams")
                if d.weekday() in (1, 3):
                    add("Design review", d, 10, 0, 11, 30, work, "Room 4B")
                    add("1:1 with Alex", d, 11, 0, 11, 30, work)
                if d.weekday() == 4:
                    add("Team lunch", d, 12, 30, 13, 45, work, "Bistro")
            if d.weekday() == 5:
                add("Gym", d, 10, 0, 11, 15, personal)
            if d.day == 10:
                add_all_day("Rent due", d, 1, personal)
            if off % 9 == 0:
                add("Dinner with friends", d, 19, 0, 21, 30, family)
            if off == 3:
                add_all_day("Weekend trip", d, 3, family)
            d += dt.timedelta(days=1)
        return out, []


# --------------------------------------------------------------------------- helpers

def week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=(d.weekday() - FIRST_WEEKDAY) % 7)


def month_grid_start(d: dt.date) -> dt.date:
    return week_start(d.replace(day=1))


def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def lighten(hex_color: str, f: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = (int(c + (255 - c) * f) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def fmt_event_time(ev: Event) -> str:
    def long_date(d):
        return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year}"

    if ev.all_day:
        last = (ev.end - dt.timedelta(days=1)).date() if ev.end > ev.start else ev.start.date()
        if last <= ev.start.date():
            return f"{long_date(ev.start.date())}  ·  All day"
        return f"{long_date(ev.start.date())}  –  {long_date(last)}"
    if ev.start.date() == ev.end.date() or ev.end - ev.start <= dt.timedelta(0):
        return f"{long_date(ev.start.date())}  ·  {ev.start:%H:%M} – {ev.end:%H:%M}"
    return f"{long_date(ev.start.date())} {ev.start:%H:%M}  –  {long_date(ev.end.date())} {ev.end:%H:%M}"


# --------------------------------------------------------------------------- settings dialog

class SettingsDialog(tk.Toplevel):
    """Account settings: optional iCloud sign-in plus iCal feed links."""

    def __init__(self, parent, app, apple_id="", has_password=False, feeds=None):
        super().__init__(parent)
        self.app = app
        self.result = None
        self.has_password = has_password
        self.title("Calendars")
        self.configure(bg=C["bg"], padx=app.px(20), pady=app.px(16))
        self.resizable(False, False)
        self.transient(parent)
        px = app.px
        wrap = px(420)

        def label(text, row, **kw):
            tk.Label(self, text=text, bg=C["bg"], justify="left", wraplength=wrap, **kw).grid(
                row=row, column=0, columnspan=2, sticky="w")

        # --- iCal feeds
        label("Calendar feed links (iCal / .ics)", 0, font=app.f_title, fg=C["text"])
        label("Paste one link per line: any calendar subscription link, e.g. Google Calendar's "
              "secret iCal address, an Outlook ICS link or your university timetable. "
              "Links starting with webcal://, https:// or http:// all work.", 1,
              font=app.f_body, fg=C["muted"])
        self.feeds_txt = tk.Text(self, width=56, height=4, font=app.f_body, wrap="none",
                                 relief="solid", bd=1, highlightthickness=0)
        self.feeds_txt.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(6), px(16)))
        if feeds:
            self.feeds_txt.insert("1.0", "\n".join(feeds))

        # --- iCloud
        label("iCloud account (optional)", 3, font=app.f_title, fg=C["text"])
        label("Apple ID email and an app-specific password (not your normal Apple password). "
              "Leave empty to use feed links only.", 4, font=app.f_body, fg=C["muted"])
        link = tk.Label(self, text="Create an app-specific password ↗", fg=C["accent"], bg=C["bg"],
                        cursor="hand2", font=app.f_body)
        link.grid(row=5, column=0, columnspan=2, sticky="w", pady=(px(2), px(8)))
        link.bind("<Button-1>", lambda e: webbrowser.open(APP_PASSWORD_URL))

        tk.Label(self, text="Apple ID", bg=C["bg"], font=app.f_body).grid(row=6, column=0, sticky="w")
        self.id_var = tk.StringVar(value=apple_id)
        ttk.Entry(self, textvariable=self.id_var, width=40).grid(row=6, column=1, sticky="ew", pady=px(3))
        tk.Label(self, text="App password", bg=C["bg"], font=app.f_body).grid(
            row=7, column=0, sticky="w", padx=(0, px(10)))
        self.pw_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.pw_var, show="•", width=40).grid(row=7, column=1, sticky="ew",
                                                                           pady=px(3))
        hint = "Saved. Leave empty to keep it." if has_password else "Format: xxxx-xxxx-xxxx-xxxx"
        tk.Label(self, text=hint, bg=C["bg"], fg=C["muted"], font=app.f_small).grid(row=8, column=1, sticky="w")

        btns = tk.Frame(self, bg=C["bg"])
        btns.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(px(16), 0))
        if apple_id or feeds:
            ttk.Button(btns, text="Remove all", command=self._sign_out).pack(side="left")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=px(6))

        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + px(60)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            self.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass
        self.grab_set()
        self.feeds_txt.focus_set()

    def _save(self):
        feeds = [normalize_feed_url(l) for l in self.feeds_txt.get("1.0", "end").splitlines() if l.strip()]
        bad = [f for f in feeds if not f.lower().startswith(("https://", "http://"))]
        if bad:
            messagebox.showwarning("Invalid link", f"This doesn't look like a calendar link:\n{bad[0][:80]}",
                                   parent=self)
            return
        aid, pw = self.id_var.get().strip(), self.pw_var.get().strip()
        if aid and not pw and not self.has_password:
            messagebox.showwarning("Missing password", "Enter the app-specific password for your Apple ID, "
                                   "or clear the Apple ID to use feed links only.", parent=self)
            return
        if not aid and not feeds:
            messagebox.showwarning("Nothing to show", "Add a calendar feed link or an iCloud account.",
                                   parent=self)
            return
        self.result = ("save", aid, pw, feeds)
        self.destroy()

    def _sign_out(self):
        self.result = ("signout", "", "", [])
        self.destroy()


# --------------------------------------------------------------------------- app

class CalendarApp:
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.scale = root.winfo_fpixels("1i") / 96.0
        self.view = tk.StringVar(value="month")
        self.cur = dt.date.today()
        self.demo = demo
        self.source = None
        self.apple_id = ""
        self._pw = ""
        self.feeds: list = []
        self.events: dict = {}
        self.by_day: dict = {}
        self.covered: list = []
        self.loading = False
        self.replace_on_next = False
        self.gen = 0
        self.q: queue.Queue = queue.Queue()
        self.chip_events: list = []
        self.need_scroll = True
        self._render_job = None

        self._fonts()
        self._style()
        self._build()
        self._keys()

        if demo:
            self.source = DemoSource()
        else:
            self._load_credentials()

        root.after(100, self._poll)
        root.after(60_000, self._tick)
        root.after(AUTO_REFRESH_MS, self._auto_refresh)
        root.after(30, self.render)
        if not demo and self.source is None:
            root.after(400, self.open_settings)

    # ---------- setup
    def px(self, v):
        return int(round(v * self.scale))

    def _fonts(self):
        F = tkfont.Font
        self.f_small = F(family="Segoe UI", size=8)
        self.f_small_b = F(family="Segoe UI Semibold", size=8)
        self.f_body = F(family="Segoe UI", size=9)
        self.f_bold = F(family="Segoe UI Semibold", size=9)
        self.f_daynum = F(family="Segoe UI", size=10)
        self.f_daybig = F(family="Segoe UI Light", size=17)
        self.f_title = F(family="Segoe UI Semibold", size=15)

    def _style(self):
        st = ttk.Style(self.root)
        st.configure("TButton", font=("Segoe UI", 9), padding=(self.px(8), self.px(3)))
        st.configure("Toolbutton", font=("Segoe UI", 9), padding=(self.px(12), self.px(4)))

    def _build(self):
        r = self.root
        r.configure(bg=C["bg"])
        top = tk.Frame(r, bg=C["toolbar"], padx=self.px(12), pady=self.px(8))
        top.pack(fill="x")
        ttk.Button(top, text="‹", width=3, command=self.prev).pack(side="left")
        ttk.Button(top, text="Today", command=self.go_today).pack(side="left", padx=self.px(4))
        ttk.Button(top, text="›", width=3, command=self.next).pack(side="left")
        self.title_lbl = tk.Label(top, font=self.f_title, bg=C["toolbar"], fg=C["text"])
        self.title_lbl.pack(side="left", padx=self.px(14))

        ttk.Button(top, text="⚙  Calendars", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="⟳  Refresh", command=self.refresh).pack(side="right", padx=self.px(6))
        seg = tk.Frame(top, bg=C["toolbar"])
        seg.pack(side="right", padx=self.px(12))
        for value, label in (("day", "Day"), ("week", "Week"), ("month", "Month")):
            ttk.Radiobutton(seg, text=label, value=value, variable=self.view, style="Toolbutton",
                            command=self._view_changed).pack(side="left")
        tk.Frame(r, bg=C["grid"], height=1).pack(fill="x")

        self.status = tk.Label(r, anchor="w", bg=C["toolbar"], fg=C["muted"], font=self.f_small,
                               padx=self.px(12), pady=self.px(3))
        self.status.pack(side="bottom", fill="x")

        body = tk.Frame(r, bg=C["bg"])
        body.pack(fill="both", expand=True)

        self.month_canvas = tk.Canvas(body, bg=C["bg"], highlightthickness=0)

        self.tg = tk.Frame(body, bg=C["bg"])
        self.tg_head = tk.Canvas(self.tg, bg=C["bg"], highlightthickness=0, height=self.px(52))
        self.tg_body = tk.Canvas(self.tg, bg=C["bg"], highlightthickness=0,
                                 yscrollincrement=self.px(24))
        sb = ttk.Scrollbar(self.tg, orient="vertical", command=self.tg_body.yview)
        self.tg_body.configure(yscrollcommand=sb.set)
        self.tg_head.grid(row=0, column=0, sticky="ew")
        tk.Frame(self.tg, bg=C["bg"]).grid(row=0, column=1, sticky="nsew")
        self.tg_body.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns")
        self.tg.rowconfigure(1, weight=1)
        self.tg.columnconfigure(0, weight=1)

        for cv in (self.month_canvas, self.tg_head, self.tg_body):
            cv.bind("<Configure>", lambda e: self._schedule_render())
            cv.bind("<Button-1>", lambda e, c=cv: self._on_click(c))
            cv.bind("<Motion>", lambda e, c=cv: self._on_motion(c))
        self.tg_body.bind("<MouseWheel>",
                          lambda e: self.tg_body.yview_scroll(int(-e.delta / 40), "units"))
        self.month_canvas.bind("<MouseWheel>",
                               lambda e: self.prev() if e.delta > 0 else self.next())

    def _keys(self):
        r = self.root
        r.bind("<Left>", lambda e: self.prev())
        r.bind("<Right>", lambda e: self.next())
        r.bind("<Home>", lambda e: self.go_today())
        r.bind("t", lambda e: self.go_today())
        r.bind("d", lambda e: self.set_view("day"))
        r.bind("w", lambda e: self.set_view("week"))
        r.bind("m", lambda e: self.set_view("month"))
        r.bind("<F5>", lambda e: self.refresh())

    # ---------- credentials
    def _load_credentials(self):
        data = read_settings()
        if data is None:
            data = self._migrate_from_credential_manager()
        aid = (data.get("apple_id") or "").strip()
        pw = (data.get("app_password") or "").strip()
        feeds = [f.strip() for f in data.get("feeds") or [] if isinstance(f, str) and f.strip()]
        log(f"app start; data folder: {DATA_DIR}; saved Apple ID: {aid or 'none'}; "
            f"password saved: {bool(pw)}; feeds: {len(feeds)}")
        self._apply_settings(aid if (aid and pw) else "", pw, feeds)

    def _migrate_from_credential_manager(self) -> dict:
        """One-time move of settings saved by older versions (Credential Manager) into the settings file."""
        aid = pw = None
        feeds = []
        try:
            import keyring
            aid = keyring.get_password(KEYRING_SERVICE, KEYRING_USER_KEY)
            pw = keyring.get_password(KEYRING_SERVICE, aid) if aid else None
            raw = keyring.get_password(KEYRING_SERVICE, KEYRING_FEEDS_KEY)
            legacy_feeds = os.path.join(LEGACY_DIR, "feeds.txt")
            if raw is None and os.path.exists(legacy_feeds):
                with open(legacy_feeds, encoding="utf-8") as fh:
                    raw = fh.read()
            feeds = [l.strip() for l in (raw or "").splitlines() if l.strip()]
        except Exception as ex:
            log(f"reading Credential Manager failed: {ex!r}")
            return {}
        if not (aid or feeds):
            return {}
        try:
            write_settings(aid or "", pw or "", feeds)
        except Exception as ex:
            log(f"could not write settings file, keeping Credential Manager: {ex!r}")
            return {"apple_id": aid, "app_password": pw, "feeds": feeds}
        for key in (aid, KEYRING_USER_KEY, KEYRING_FEEDS_KEY):
            if key:
                try:
                    keyring.delete_password(KEYRING_SERVICE, key)
                except Exception:
                    pass
        for old in ("feeds.txt", "log.txt", "log.txt.old"):
            try:
                os.remove(os.path.join(LEGACY_DIR, old))
            except OSError:
                pass
        try:
            os.rmdir(LEGACY_DIR)
        except OSError:
            pass
        log(f"moved settings from Credential Manager to {SETTINGS_FILE}")
        return {"apple_id": aid, "app_password": pw, "feeds": feeds}

    def _apply_settings(self, aid, pw, feeds):
        self.apple_id, self._pw, self.feeds = aid, pw, list(feeds)
        icloud = ICloudSource(aid, pw) if aid and pw else None
        self.source = CombinedSource(icloud, self.feeds) if (icloud or self.feeds) else None

    def _store_settings(self, aid, pw, feeds):
        write_settings(aid, pw, feeds)

    def open_settings(self):
        if self.demo:
            messagebox.showinfo(APP_NAME, "Running with demo data. Start without --demo to sign in.",
                                parent=self.root)
            return
        dlg = SettingsDialog(self.root, self, self.apple_id, bool(self._pw), self.feeds)
        self.root.wait_window(dlg)
        if not dlg.result:
            if self.source is None:
                self.set_status("No calendars yet. Click Calendars to add a feed link or iCloud account.")
            return
        action, aid, pw, feeds = dlg.result
        if action == "save" and aid and not pw and aid == self.apple_id:
            pw = self._pw  # keep the saved password
        if not aid:
            pw = ""
        try:
            self._store_settings(aid, pw, feeds)
        except Exception as ex:
            messagebox.showwarning(APP_NAME, f"Couldn't save settings:\n{ex}", parent=self.root)
        self._apply_settings(aid, pw, feeds)
        log(f"settings saved; Apple ID: {aid or 'none'}; feeds: {len(feeds)}")
        if self.source is None:
            self.set_status("All calendars removed.")
        self._reset_data(keep_visible=False)
        self.render()

    # ---------- data
    def _reset_data(self, keep_visible: bool):
        self.gen += 1
        self.covered = []
        if keep_visible:
            self.replace_on_next = True
        else:
            self.events, self.by_day = {}, {}
            self.replace_on_next = False

    def refresh(self):
        if self.source is None:
            self.open_settings()
            return
        self.source.reset()  # re-download feeds, pick up new / renamed calendars
        self._reset_data(keep_visible=True)
        self.render()

    def _auto_refresh(self):
        if self.source is not None and not self.loading:
            self.refresh()
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _tick(self):
        if self.view.get() != "month" or dt.date.today() != getattr(self, "_last_today", None):
            self.render()
        self.root.after(60_000, self._tick)

    def ensure_range(self, s: dt.date, e: dt.date):
        if self.source is None or self.loading:
            return
        if any(cs <= s and e <= ce for cs, ce in self.covered):
            return
        fs = month_grid_start(self.cur) - dt.timedelta(days=7)
        fe = month_grid_start(self.cur) + dt.timedelta(days=49)
        fs, fe = min(fs, s), max(fe, e)
        self.loading = True
        self.set_status("Loading events…")
        threading.Thread(target=self._worker, args=(self.source, self.gen, fs, fe), daemon=True).start()

    def _worker(self, src, gen, fs, fe):
        try:
            evs, warns = src.fetch(dt.datetime.combine(fs, dt.time.min), dt.datetime.combine(fe, dt.time.min))
            self.q.put(("ok", gen, (fs, fe), evs, warns))
        except Exception as ex:
            log(f"fetch failed: {type(ex).__module__}.{type(ex).__name__}: {ex}\n{traceback.format_exc()}")
            self.q.put(("err", gen, (fs, fe), ex, None))

    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, msg):
        kind, gen, rng, payload, warns = msg
        self.loading = False
        if gen != self.gen:
            self.render()
            return
        self.covered.append(rng)
        if kind == "ok":
            if self.replace_on_next:
                self.events = {}
                self.replace_on_next = False
            for ev in payload:
                self.events[ev.key()] = ev
            self._reindex()
            ncal = getattr(self.source, "calendar_count", 0)
            note = (f"Updated {dt.datetime.now():%H:%M}  ·  {self.source.label if self.source else ''}"
                    f"  ·  {len(payload)} event(s)" + (f" from {ncal} calendar(s)" if ncal else ""))
            if warns:
                note += f"  ·  {len(warns)} item(s) skipped: {warns[0][:80]}"
            self.set_status(note)
        else:
            ex = payload
            text = str(ex) or type(ex).__name__
            auth = "Authoriz" in type(ex).__name__ or "401" in text or "Unauthorized" in text
            if auth:
                self.set_status("Sign-in failed. Check your Apple ID and app-specific password.")
                if messagebox.askyesno(APP_NAME, "iCloud rejected the sign-in.\n\nMake sure you're using an "
                                       "app-specific password, not your normal Apple password.\n\n"
                                       "Open account settings?", parent=self.root):
                    self.root.after(10, self.open_settings)
            else:
                self.set_status(f"Couldn't load events: {text[:160]}")
        self.render()

    def _reindex(self):
        by = {}
        for ev in self.events.values():
            d = ev.start.date()
            last = (ev.end - dt.timedelta(microseconds=1)).date() if ev.end > ev.start else d
            n = 0
            while d <= last and n < 400:
                by.setdefault(d, []).append(ev)
                d += dt.timedelta(days=1)
                n += 1
        for lst in by.values():
            lst.sort(key=lambda e: (not e.all_day, e.start, e.title.lower()))
        self.by_day = by

    def events_on(self, d: dt.date):
        return self.by_day.get(d, [])

    def set_status(self, text):
        self.status.configure(text=text)

    # ---------- navigation
    def view_range(self):
        v = self.view.get()
        if v == "month":
            s = month_grid_start(self.cur)
            return s, s + dt.timedelta(days=42)
        if v == "week":
            s = week_start(self.cur)
            return s, s + dt.timedelta(days=7)
        return self.cur, self.cur + dt.timedelta(days=1)

    def prev(self):
        self._move(-1)

    def next(self):
        self._move(1)

    def _move(self, n):
        v = self.view.get()
        if v == "month":
            self.cur = add_months(self.cur, n)
        elif v == "week":
            self.cur += dt.timedelta(days=7 * n)
        else:
            self.cur += dt.timedelta(days=n)
        self.need_scroll = True
        self.render()

    def go_today(self):
        self.cur = dt.date.today()
        self.need_scroll = True
        self.render()

    def set_view(self, v, day: dt.date | None = None):
        if day:
            self.cur = day
        self.view.set(v)
        self._view_changed()

    def _view_changed(self):
        self.need_scroll = True
        self.render()

    # ---------- rendering
    def _schedule_render(self):
        if self._render_job:
            self.root.after_cancel(self._render_job)
        self._render_job = self.root.after(25, self.render)

    def render(self):
        self._render_job = None
        self._last_today = dt.date.today()
        self.chip_events = []
        v = self.view.get()
        self.title_lbl.configure(text=self._title())
        if v == "month":
            self.tg.pack_forget()
            self.month_canvas.pack(fill="both", expand=True)
            self._draw_month()
        else:
            self.month_canvas.pack_forget()
            self.tg.pack(fill="both", expand=True)
            days = [week_start(self.cur) + dt.timedelta(days=i) for i in range(7)] if v == "week" else [self.cur]
            self._draw_timegrid(days)
        self.ensure_range(*self.view_range())

    def _title(self):
        v, d = self.view.get(), self.cur
        if v == "month":
            return f"{MONTHS[d.month - 1]} {d.year}"
        if v == "day":
            return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year}"
        s = week_start(d)
        e = s + dt.timedelta(days=6)
        if s.month == e.month:
            return f"{s.day} – {e.day} {MONTHS[s.month - 1]} {s.year}"
        if s.year == e.year:
            return f"{s.day} {MONTHS_SHORT[s.month - 1]} – {e.day} {MONTHS_SHORT[e.month - 1]} {e.year}"
        return f"{s.day} {MONTHS_SHORT[s.month - 1]} {s.year} – {e.day} {MONTHS_SHORT[e.month - 1]} {e.year}"

    def _fit(self, text, font, width):
        if width <= 0:
            return ""
        if font.measure(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…" if lo > 0 else ""

    def _ev_tag(self, ev):
        self.chip_events.append(ev)
        return f"ev:{len(self.chip_events) - 1}"

    def _draw_month(self):
        c = self.month_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        px = self.px
        head = px(30)
        cw, ch = w / 7, (h - head) / 6
        for i in range(7):
            c.create_text(i * cw + px(10), head / 2, anchor="w", font=self.f_small_b, fill=C["muted"],
                          text=WEEKDAYS_SHORT[(i + FIRST_WEEKDAY) % 7].upper())
        c.create_line(0, head - 1, w, head - 1, fill=C["grid"])
        start, today = month_grid_start(self.cur), dt.date.today()
        line_h = px(19)
        for idx in range(42):
            d = start + dt.timedelta(days=idx)
            r, col = divmod(idx, 7)
            x0, y0 = col * cw, head + r * ch
            x1, y1 = x0 + cw, y0 + ch
            in_month = d.month == self.cur.month
            fill = C["today_bg"] if d == today else (C["bg"] if in_month else C["out_month"])
            tag = f"day:{d.isoformat()}"
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=C["grid"], tags=(tag,))
            nx, ny = x0 + px(15), y0 + px(14)
            if d == today:
                c.create_oval(nx - px(11), ny - px(11), nx + px(11), ny + px(11), fill=C["accent"],
                              outline="", tags=(tag,))
                num_fg = "#ffffff"
            else:
                num_fg = C["text"] if in_month else C["muted"]
            c.create_text(nx, ny, text=str(d.day), font=self.f_daynum, fill=num_fg, tags=(tag,))
            if d.day == 1 and d != today:
                c.create_text(nx + px(12), ny, anchor="w", text=MONTHS_SHORT[d.month - 1],
                              font=self.f_small, fill=C["muted"], tags=(tag,))

            evs = self.events_on(d)
            top = y0 + px(29)
            avail = max(int((y1 - top - px(2)) // line_h), 0)
            shown = evs if len(evs) <= avail else evs[:max(avail - 1, 0)]
            y = top
            for ev in shown:
                self._month_chip(c, ev, d, x0 + px(4), y, cw - px(8), line_h - px(2), fill)
                y += line_h
            if len(shown) < len(evs):
                c.create_text(x0 + px(8), y + (line_h - px(2)) / 2, anchor="w", font=self.f_small_b,
                              fill=C["muted"], text=f"+{len(evs) - len(shown)} more", tags=(tag,))

    def _month_chip(self, c, ev, d, x, y, wd, ht, cell_bg):
        px = self.px
        tag = self._ev_tag(ev)
        multi_day = ev.end - ev.start >= dt.timedelta(hours=24)
        if ev.all_day or multi_day:
            c.create_rectangle(x, y, x + wd, y + ht, fill=lighten(ev.color, 0.78), outline="", tags=(tag,))
            c.create_rectangle(x, y, x + px(3), y + ht, fill=ev.color, outline="", tags=(tag,))
            c.create_text(x + px(8), y + ht / 2, anchor="w", font=self.f_small_b, fill=C["text"],
                          text=self._fit(ev.title, self.f_small_b, wd - px(11)), tags=(tag,))
        else:
            c.create_rectangle(x, y, x + wd, y + ht, fill=cell_bg, outline="", tags=(tag,))
            cy = y + ht / 2
            c.create_oval(x + px(3), cy - px(3), x + px(9), cy + px(3), fill=ev.color, outline="", tags=(tag,))
            t = f"{ev.start:%H:%M}" if ev.start.date() == d else "…"
            label = self._fit(f"{t}  {ev.title}", self.f_small, wd - px(15))
            c.create_text(x + px(13), cy, anchor="w", font=self.f_small, fill=C["text"], text=label, tags=(tag,))

    def _draw_timegrid(self, days):
        px = self.px
        hc, bc = self.tg_head, self.tg_body
        hc.delete("all")
        bc.delete("all")
        w = bc.winfo_width()
        if w < 50:
            return
        today = dt.date.today()
        gut, hour_h = px(56), px(48)
        n = len(days)
        colw = (w - gut) / n

        # header: day labels + all-day events
        allday = [[e for e in self.events_on(d) if e.all_day or e.end - e.start >= dt.timedelta(hours=24)]
                  for d in days]
        rows = max((len(a) for a in allday), default=0)
        row_h = px(20)
        base = px(54)
        head_h = base + (rows * row_h + px(6) if rows else 0)
        hc.configure(height=head_h)
        if rows:
            hc.create_text(gut - px(8), base + px(10), anchor="e", text="all-day", font=self.f_small,
                           fill=C["muted"])
        for i, d in enumerate(days):
            x0 = gut + i * colw
            tag = f"day:{d.isoformat()}"
            is_today = d == today
            if is_today:
                hc.create_rectangle(x0, 0, x0 + colw, head_h, fill=C["today_bg"], outline="", tags=(tag,))
            else:
                hc.create_rectangle(x0, 0, x0 + colw, head_h, fill=C["bg"], outline="", tags=(tag,))
            cx = x0 + colw / 2
            hc.create_text(cx, px(13), text=WEEKDAYS_SHORT[d.weekday()].upper(), font=self.f_small_b,
                           fill=C["accent"] if is_today else C["muted"], tags=(tag,))
            if is_today:
                hc.create_oval(cx - px(16), px(20), cx + px(16), px(52), fill=C["accent"], outline="", tags=(tag,))
            hc.create_text(cx, px(36), text=str(d.day), font=self.f_daybig,
                           fill="#ffffff" if is_today else C["text"], tags=(tag,))
            hc.create_line(x0, px(40), x0, head_h, fill=C["grid"])
            for j, ev in enumerate(allday[i]):
                y = base + j * row_h
                et = self._ev_tag(ev)
                hc.create_rectangle(x0 + px(2), y, x0 + colw - px(2), y + row_h - px(2),
                                    fill=lighten(ev.color, 0.78), outline="", tags=(et,))
                hc.create_rectangle(x0 + px(2), y, x0 + px(5), y + row_h - px(2), fill=ev.color, outline="",
                                    tags=(et,))
                hc.create_text(x0 + px(9), y + (row_h - px(2)) / 2, anchor="w", font=self.f_small_b,
                               fill=C["text"], text=self._fit(ev.title, self.f_small_b, colw - px(14)),
                               tags=(et,))
        hc.create_line(0, head_h - 1, hc.winfo_width() or w, head_h - 1, fill=C["grid"])

        # body: hour grid
        total = 24 * hour_h
        bc.configure(scrollregion=(0, 0, w, total))
        for i, d in enumerate(days):
            if d == today:
                x0 = gut + i * colw
                bc.create_rectangle(x0, 0, x0 + colw, total, fill=C["today_bg"], outline="")
        for hr in range(24):
            y = hr * hour_h
            bc.create_line(gut - px(6), y, w, y, fill=C["grid"])
            bc.create_line(gut, y + hour_h / 2, w, y + hour_h / 2, fill=C["grid_soft"])
            if hr:
                bc.create_text(gut - px(9), y, anchor="e", text=f"{hr:02d}:00", font=self.f_small,
                               fill=C["muted"])
        for i in range(n + 1):
            x = gut + i * colw
            bc.create_line(x, 0, x, total, fill=C["grid"])

        for i, d in enumerate(days):
            x0 = gut + i * colw
            day_s = dt.datetime.combine(d, dt.time.min)
            day_e = day_s + dt.timedelta(days=1)
            items = []
            for ev in self.events_on(d):
                if ev.all_day or ev.end - ev.start >= dt.timedelta(hours=24):
                    continue
                s = max(ev.start, day_s)
                e = min(ev.end, day_e)
                sm = (s - day_s).total_seconds() / 60
                em = max((e - day_s).total_seconds() / 60, sm + 20)
                items.append((sm, em, ev))
            for (sm, em, ev), col, ncols in self._layout(items):
                inner = colw - px(6)
                cw_ = inner / ncols
                x = x0 + px(3) + col * cw_
                wd = cw_ - px(2)
                y0 = sm / 60 * hour_h + 1
                y1 = min(em / 60 * hour_h - 1, total)
                tag = self._ev_tag(ev)
                bc.create_rectangle(x, y0, x + wd, y1, fill=lighten(ev.color, 0.8),
                                    outline=lighten(ev.color, 0.55), tags=(tag,))
                bc.create_rectangle(x, y0, x + px(3), y1, fill=ev.color, outline="", tags=(tag,))
                tw = wd - px(10)
                hgt = y1 - y0
                if hgt < px(30):
                    label = self._fit(f"{ev.title}  {ev.start:%H:%M}", self.f_small_b, tw)
                    bc.create_text(x + px(7), y0 + hgt / 2, anchor="w", text=label, font=self.f_small_b,
                                   fill=C["text"], tags=(tag,))
                else:
                    bc.create_text(x + px(7), y0 + px(3), anchor="nw", font=self.f_small_b, fill=C["text"],
                                   text=self._fit(ev.title, self.f_small_b, tw), tags=(tag,))
                    sub = f"{ev.start:%H:%M} – {ev.end:%H:%M}"
                    if ev.location and hgt >= px(46):
                        bc.create_text(x + px(7), y0 + px(32), anchor="nw", font=self.f_small,
                                       fill=C["muted"], text=self._fit(ev.location, self.f_small, tw),
                                       tags=(tag,))
                    bc.create_text(x + px(7), y0 + px(17), anchor="nw", font=self.f_small, fill=C["muted"],
                                   text=self._fit(sub, self.f_small, tw), tags=(tag,))

        now = dt.datetime.now()
        if now.date() in days:
            i = days.index(now.date())
            y = (now.hour * 60 + now.minute) / 60 * hour_h
            x0 = gut + i * colw
            bc.create_line(x0, y, x0 + colw, y, fill=C["now"], width=max(px(2), 2))
            bc.create_oval(x0 - px(4), y - px(4), x0 + px(4), y + px(4), fill=C["now"], outline="")

        if self.need_scroll:
            self.need_scroll = False
            target = (now.hour - 1) if now.date() in days else 7
            first = min((ev.start.hour for d in days for ev in self.events_on(d)
                         if not ev.all_day and ev.start.date() == d), default=target)
            target = max(min(target, first), 0)
            bc.yview_moveto(max(target - 0.4, 0) / 24)

    @staticmethod
    def _layout(items):
        """Assign side-by-side columns to overlapping events."""
        items = sorted(items, key=lambda t: (t[0], -t[1]))
        out, cluster, cols, cluster_end = [], [], [], -1.0
        for it in items:
            s, e = it[0], it[1]
            if cluster and s >= cluster_end:
                out += [(c_it, c, len(cols)) for c_it, c in cluster]
                cluster, cols, cluster_end = [], [], -1.0
            for ci, col_end in enumerate(cols):
                if col_end <= s:
                    cols[ci] = e
                    cluster.append((it, ci))
                    break
            else:
                cols.append(e)
                cluster.append((it, len(cols) - 1))
            cluster_end = max(cluster_end, e)
        out += [(c_it, c, len(cols)) for c_it, c in cluster]
        return out

    # ---------- interaction
    def _tags_under(self, canvas):
        item = canvas.find_withtag("current")
        return canvas.gettags(item[0]) if item else ()

    def _on_motion(self, canvas):
        tags = self._tags_under(canvas)
        hot = any(t.startswith("ev:") or t.startswith("day:") for t in tags)
        canvas.configure(cursor="hand2" if hot else "")

    def _on_click(self, canvas):
        for t in self._tags_under(canvas):
            if t.startswith("ev:"):
                self.show_event(self.chip_events[int(t[3:])])
                return
        for t in self._tags_under(canvas):
            if t.startswith("day:"):
                d = dt.date.fromisoformat(t[4:])
                if self.view.get() != "day" or d != self.cur:
                    self.set_view("day", d)
                return

    def show_event(self, ev: Event):
        lines = [fmt_event_time(ev), f"Calendar: {ev.calendar}"]
        if ev.location:
            lines.append(f"Location: {ev.location}")
        if ev.notes:
            notes = ev.notes if len(ev.notes) < 800 else ev.notes[:800] + "…"
            lines += ["", notes]
        messagebox.showinfo(ev.title, "\n".join(lines), parent=self.root)


# --------------------------------------------------------------------------- main

def _selftest(path):
    """Build check: confirm bundled libraries import (writes result to `path`)."""
    lines = []
    for mod in ("caldav", "icalendar", "recurring_ical_events", "keyring", "tzdata"):
        try:
            __import__(mod)
            lines.append(f"ok {mod}")
        except Exception as ex:
            lines.append(f"FAIL {mod}: {ex}")
    try:
        import keyring
        lines.append(f"keyring backend: {keyring.get_keyring()!r}")
    except Exception as ex:
        lines.append(f"FAIL keyring backend: {ex}")
    try:
        import urllib.request
        with urllib.request.urlopen("https://www.apple.com/", timeout=20) as r:
            lines.append(f"ok https download (status {r.status})")
    except Exception as ex:
        lines.append(f"FAIL https download: {ex}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    if "--selftest" in sys.argv:
        _selftest(sys.argv[sys.argv.index("--selftest") + 1])
        return
    try:  # crisp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title(APP_NAME)
    try:
        root.iconbitmap(resource_path("app.ico"))
    except Exception:
        pass
    app = CalendarApp(root, demo="--demo" in sys.argv)
    w, h = app.px(1120), app.px(760)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(w, sw - 40), min(h, sh - 80)
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max((sh - h) // 2 - 20, 0)}")
    root.minsize(app.px(640), app.px(460))
    root.mainloop()


if __name__ == "__main__":
    main()
