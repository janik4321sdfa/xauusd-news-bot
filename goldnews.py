#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GoldNewsBot - XAUUSD / Gold economic news watcher -> Discord

Zdroje dat:
  1) Forex Factory weekly JSON feed (nfs.faireconomy.media)  [PRIMARY]
  2) TradingView economic calendar                            [FALLBACK]

Rezimy:
  --digest     posle dnesni souhrn (High + Medium) a skonci
  --watch      hlidaci beh: auto denni souhrn + upozorneni 5 min pred zpravou
  --now        okamzity souhrn na vyzadani (pro ikonu na plose) - ignoruje dedup
  --selftest   interni testy bez odesilani
  --dry        nic neposila, jen vypise co by poslal
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------
# Timezone: Europe/Prague, s fallbackem bez zavislosti (EU DST pravidla)
# ----------------------------------------------------------------------------
def _prague_fallback():
    class Prague(dt.tzinfo):
        def _last_sunday(self, year, month):
            d = dt.date(year, month, 31)
            while d.month != month:
                d -= dt.timedelta(days=1)
            while d.weekday() != 6:
                d -= dt.timedelta(days=1)
            return d

        def _is_dst(self, d):
            y = d.year
            start = dt.datetime.combine(self._last_sunday(y, 3), dt.time(1, 0))
            end = dt.datetime.combine(self._last_sunday(y, 10), dt.time(1, 0))
            naive_utc = d.replace(tzinfo=None)
            return start <= naive_utc < end

        def utcoffset(self, d):
            if d is None:
                return dt.timedelta(hours=1)
            return dt.timedelta(hours=2 if self._is_dst(d) else 1)

        def dst(self, d):
            if d is None:
                return dt.timedelta(0)
            return dt.timedelta(hours=1 if self._is_dst(d) else 0)

        def tzname(self, d):
            return "CEST" if (d and self._is_dst(d)) else "CET"

    return Prague()


try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Prague")
except Exception:
    LOCAL_TZ = _prague_fallback()

UTC = dt.timezone.utc

# ----------------------------------------------------------------------------
# Konfigurace
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("GOLDNEWS_STATE", os.path.join(HERE, "state.json"))
CACHE_PATH = os.environ.get("GOLDNEWS_CACHE", os.path.join(HERE, "ff_cache.json"))
LOG_DIR = os.environ.get("GOLDNEWS_LOGS", os.path.join(HERE, "logs"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_MIN_CACHE_SEC = 3600          # FF feed rate-limituje -> min. 1h cache
FF_MAX_STALE_SEC = 20 * 3600     # nad tuto hranici uz FF cache nepovazujeme za spolehlivou
TV_URL = "https://economic-calendar.tradingview.com/events"

# Meny relevantni pro XAUUSD (zlato se kotuje v USD; "All" = globalni eventy)
GOLD_CURRENCIES = {"USD", "All"}
WANTED_IMPACTS = ("High", "Medium")

PING_LEAD_MIN = 5                # kolik minut pred zpravou upozornit
PING_WINDOW_MIN = 8              # horni hranice okna (< 10 = zadne duplikaty pri cron */5)
DIGEST_HOUR = 7                  # denni souhrn v 07:00 lokalne
DIGEST_MINUTE = 0

COLOR_HIGH = 0xE74C3C
COLOR_MED = 0xE67E22
COLOR_INFO = 0xF1C40F
COLOR_CALM = 0x2ECC71

IMPACT_ICON = {"High": "🔴", "Medium": "🟠", "Low": "⚪"}

# odkud bot bezi (nastavuje CI / planovac) - jen informace do footeru
ORIGIN = os.environ.get("GOLDNEWS_ORIGIN", "").strip()


def foot(source):
    base = f"Zdroj: {source} | GoldNewsBot"
    return base + (f" | {ORIGIN}" if ORIGIN else "")


def webhook_url():
    u = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if u:
        return u
    cfg = os.path.join(HERE, "config.json")
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8-sig") as f:
                return (json.load(f).get("discord_webhook") or "").strip()
        except Exception:
            pass
    return ""


# ----------------------------------------------------------------------------
# Logovani
# ----------------------------------------------------------------------------
def log(msg):
    ts = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fn = os.path.join(LOG_DIR, dt.datetime.now(LOCAL_TZ).strftime("%Y-%m") + ".log")
        with open(fn, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http_json(url, headers=None, timeout=30, retries=3, backoff=4):
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
         "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as ex:
            last = ex
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    raise last


# ----------------------------------------------------------------------------
# Normalizovany event
#   {id, title, currency, impact, when(aware dt), forecast, previous, source}
# ----------------------------------------------------------------------------
def mk_id(when_utc, title, currency):
    key = f"{when_utc.strftime('%Y%m%dT%H%M')}|{currency}|{title.strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def fetch_forexfactory():
    """Weekly FF feed s povinnou cache (feed vraci 429 pri castych dotazech).
    Vraci (events, age_seconds) - age = jak stara jsou pouzita data."""
    now = time.time()
    cached = None
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8-sig") as f:
                cached = json.load(f)
        except Exception:
            cached = None

    age = 0.0
    if cached and (now - cached.get("fetched_at", 0)) < FF_MIN_CACHE_SEC and cached.get("data"):
        age = now - cached["fetched_at"]
        log(f"FF: pouzivam cache ({int(age)}s stara, {len(cached['data'])} eventu)")
        raw = cached["data"]
    else:
        try:
            raw = http_json(FF_URL, retries=2, backoff=5)
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now, "data": raw}, f)
            age = 0.0
            log(f"FF: nacteno online ({len(raw)} eventu), cache aktualizovana")
        except Exception as ex:
            if cached and cached.get("data"):
                age = now - cached.get("fetched_at", 0)
                log(f"FF: online selhalo ({type(ex).__name__}) -> stara cache "
                    f"({age / 3600:.1f}h)")
                raw = cached["data"]
            else:
                raise

    out = []
    for e in raw:
        try:
            cur = (e.get("country") or "").strip()
            imp = (e.get("impact") or "").strip()
            when = dt.datetime.fromisoformat(e["date"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            wu = when.astimezone(UTC)
            out.append({
                "id": mk_id(wu, e.get("title", ""), cur),
                "title": (e.get("title") or "").strip(),
                "currency": cur,
                "impact": imp,
                "when": wu,
                "forecast": (e.get("forecast") or "").strip(),
                "previous": (e.get("previous") or "").strip(),
                "source": "ForexFactory",
            })
        except Exception:
            continue
    return out, age


TV_IMPORTANCE = {1: "High", 0: "Medium", -1: "Low"}


def fetch_tradingview(days_back=1, days_fwd=2):
    """Fallback zdroj - TradingView economic calendar (US)."""
    now_l = dt.datetime.now(LOCAL_TZ)
    frm = (dt.datetime.combine(now_l.date(), dt.time(0, 0), LOCAL_TZ)
           - dt.timedelta(days=days_back)).astimezone(UTC)
    to = frm + dt.timedelta(days=days_back + days_fwd)
    url = (f"{TV_URL}?from={frm.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
           f"&to={to.strftime('%Y-%m-%dT%H:%M:%S.000Z')}&countries=US")
    j = http_json(url, headers={"Origin": "https://www.tradingview.com",
                                "Referer": "https://www.tradingview.com/"})
    out = []
    for e in j.get("result", []):
        try:
            wu = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(UTC)
            imp = TV_IMPORTANCE.get(e.get("importance"), "Low")
            def fmt(v):
                return "" if v is None else str(v)
            out.append({
                "id": mk_id(wu, e.get("title", ""), "USD"),
                "title": (e.get("title") or "").strip(),
                "currency": "USD",
                "impact": imp,
                "when": wu,
                "forecast": fmt(e.get("forecast")),
                "previous": fmt(e.get("previous")),
                "source": "TradingView",
            })
        except Exception:
            continue
    log(f"TV: nacteno {len(out)} US eventu")
    return out


def get_events():
    """FF primarne. Kdyz je FF cache prilis stara (nebo FF uplne padne),
    prepne se na TradingView. Nikdy neposila tyden stara data jako aktualni."""
    ff, age = None, None
    try:
        ff, age = fetch_forexfactory()
    except Exception as ex:
        log(f"FF: NEDOSTUPNE ({type(ex).__name__}: {ex})")

    if ff and age is not None and age <= FF_MAX_STALE_SEC:
        return ff, "ForexFactory"

    if ff:
        log(f"FF: cache je {age / 3600:.1f}h stara (limit {FF_MAX_STALE_SEC / 3600:.0f}h)"
            f" -> zkousim TradingView")
    try:
        tv = fetch_tradingview()
        if tv:
            return tv, "TradingView (zaloha)"
    except Exception as ex:
        log(f"TV: NEDOSTUPNE ({type(ex).__name__}: {ex})")

    if ff:
        log("Oba zdroje problematicke -> pouzivam starou FF cache")
        return ff, f"ForexFactory (cache {age / 3600:.0f}h)"

    raise RuntimeError("zadny zdroj dat neni dostupny")


def gold_relevant(events):
    return [e for e in events
            if e["currency"] in GOLD_CURRENCIES and e["impact"] in WANTED_IMPACTS]


def events_for_day(events, day, tz=LOCAL_TZ):
    out = [e for e in events if e["when"].astimezone(tz).date() == day]
    return sorted(out, key=lambda x: x["when"])


# ----------------------------------------------------------------------------
# Stav
# ----------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8-sig") as f:
                s = json.load(f)
                s.setdefault("digest_sent_for", "")
                s.setdefault("pinged", [])
                return s
        except Exception:
            pass
    return {"digest_sent_for": "", "pinged": []}


def save_state(s):
    s["pinged"] = s.get("pinged", [])[-400:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE_PATH)


# ----------------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------------
def discord_send(embed, dry=False, content=None):
    url = webhook_url()
    if not url:
        log("CHYBA: chybi DISCORD_WEBHOOK / config.json")
        return False
    payload = {"username": "XAUUSD News Bot", "embeds": [embed]}
    if content:
        payload["content"] = content
    if dry:
        log("DRY-RUN, neposilam:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return True
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "GoldNewsBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status in (200, 204):
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 3.0
                log(f"Discord 429, cekam {wait}s")
                time.sleep(min(wait + 0.5, 15))
                continue
            log(f"Discord HTTP {e.code}: {e.read()[:200]!r}")
        except Exception as ex:
            log(f"Discord chyba: {type(ex).__name__}: {ex}")
        time.sleep(2 + attempt * 2)
    return False


def fmt_line(e):
    t = e["when"].astimezone(LOCAL_TZ).strftime("%H:%M")
    icon = IMPACT_ICON.get(e["impact"], "⚪")
    extra = []
    if e.get("forecast"):
        extra.append(f"prognoza {e['forecast']}")
    if e.get("previous"):
        extra.append(f"predchozi {e['previous']}")
    tail = f"  _( {' | '.join(extra)} )_" if extra else ""
    return f"{icon} **{t}**  `{e['currency']}`  {e['title']}{tail}"


def build_digest_embed(day, evs, source):
    ds = day.strftime("%d.%m.%Y")
    if not evs:
        return {
            "title": f"XAUUSD - {ds}: zadne High/Medium zpravy",
            "description": ("Dnes nejsou naplanovane zadne High ani Medium impact "
                            "USD zpravy. Klidny den pro zlato."),
            "color": COLOR_CALM,
            "footer": {"text": foot(source)},
        }
    highs = [e for e in evs if e["impact"] == "High"]
    meds = [e for e in evs if e["impact"] == "Medium"]
    parts = []
    if highs:
        parts.append("__**HIGH IMPACT**__\n" + "\n".join(fmt_line(e) for e in highs))
    if meds:
        parts.append("__**MEDIUM IMPACT**__\n" + "\n".join(fmt_line(e) for e in meds))
    return {
        "title": f"XAUUSD / GOLD - zpravy na {ds}",
        "description": "\n\n".join(parts),
        "color": COLOR_HIGH if highs else COLOR_MED,
        "fields": [{
            "name": "Souhrn",
            "value": f"High: **{len(highs)}**   |   Medium: **{len(meds)}**   "
                     f"|   celkem **{len(evs)}**",
        }],
        "footer": {"text": foot(source) + " | casy Europe/Prague"},
    }


def build_ping_embed(evs, minutes, source):
    """evs = seznam eventu se STEJNYM casem -> jedna zprava misto nekolika."""
    if not isinstance(evs, list):
        evs = [evs]
    evs = sorted(evs, key=lambda x: (x["impact"] != "High", x["title"]))
    when = evs[0]["when"]
    t = when.astimezone(LOCAL_TZ).strftime("%H:%M")
    has_high = any(e["impact"] == "High" for e in evs)
    icon = IMPACT_ICON["High"] if has_high else IMPACT_ICON["Medium"]
    m = max(0, int(round(minutes)))

    if len(evs) == 1:
        e = evs[0]
        title = f"{icon} ZA {m} MIN - {e['title']}"
        desc = (f"**{e['impact']} impact** | `{e['currency']}` | "
                f"vychazi v **{t}** (Praha)\n"
                f"Pripravena volatilita na **XAUUSD**.")
        fields = []
        if e.get("forecast"):
            fields.append({"name": "Prognoza", "value": e["forecast"], "inline": True})
        if e.get("previous"):
            fields.append({"name": "Predchozi", "value": e["previous"], "inline": True})
    else:
        title = f"{icon} ZA {m} MIN - {len(evs)} zpravy v {t}"
        lines = []
        for e in evs:
            bits = []
            if e.get("forecast"):
                bits.append(f"prognoza {e['forecast']}")
            if e.get("previous"):
                bits.append(f"predchozi {e['previous']}")
            tail = f"  _( {' | '.join(bits)} )_" if bits else ""
            lines.append(f"{IMPACT_ICON.get(e['impact'], '')} **{e['impact']}** "
                         f"`{e['currency']}` {e['title']}{tail}")
        desc = (f"Vsechny vychazi v **{t}** (Praha).\n"
                f"Pripravena volatilita na **XAUUSD**.\n\n" + "\n".join(lines))
        fields = []

    return {
        "title": title,
        "description": desc,
        "color": COLOR_HIGH if has_high else COLOR_MED,
        "fields": fields,
        "footer": {"text": foot(source)},
    }


# ----------------------------------------------------------------------------
# Akce
# ----------------------------------------------------------------------------
def do_digest(dry=False, force=False):
    events, source = get_events()
    day = dt.datetime.now(LOCAL_TZ).date()
    evs = events_for_day(gold_relevant(events), day)
    st = load_state()
    key = day.isoformat()
    if not force and st.get("digest_sent_for") == key:
        log(f"Denni souhrn pro {key} jiz byl odeslan - preskakuji")
        return True
    ok = discord_send(build_digest_embed(day, evs, source), dry=dry)
    log(f"Denni souhrn {key}: {len(evs)} eventu -> {'OK' if ok else 'SELHALO'}")
    if ok and not dry and not force:
        st["digest_sent_for"] = key
        save_state(st)
    return ok


def do_watch(dry=False, allow_sleep=True):
    """Jeden hlidaci pruchod: denni souhrn (je-li cas) + pingy 5 min predem."""
    events, source = get_events()
    rel = gold_relevant(events)
    st = load_state()
    now = dt.datetime.now(LOCAL_TZ)
    today = now.date()
    changed = False

    # 1) denni souhrn
    if (st.get("digest_sent_for") != today.isoformat()
            and (now.hour, now.minute) >= (DIGEST_HOUR, DIGEST_MINUTE)):
        evs = events_for_day(rel, today)
        if discord_send(build_digest_embed(today, evs, source), dry=dry):
            log(f"Denni souhrn odeslan ({len(evs)} eventu)")
            if not dry:
                st["digest_sent_for"] = today.isoformat()
                changed = True

    # 2) pingy pred zpravou - eventy se stejnym casem se sloucí do jedne zpravy
    pinged = set(st.get("pinged", []))
    now_u = dt.datetime.now(UTC)
    groups = {}
    for e in rel:
        if e["id"] in pinged:
            continue
        mins = (e["when"] - now_u).total_seconds() / 60.0
        if 0.0 < mins <= PING_WINDOW_MIN:
            groups.setdefault(e["when"], []).append(e)

    for when in sorted(groups):
        grp = groups[when]
        mins = (when - dt.datetime.now(UTC)).total_seconds() / 60.0
        # presnost: je-li jeste cas, dospi na presne T-PING_LEAD_MIN
        if allow_sleep and mins > PING_LEAD_MIN + 0.25:
            wait = (mins - PING_LEAD_MIN) * 60.0
            log(f"Cekam {wait:.0f}s na presny T-{PING_LEAD_MIN}min "
                f"({len(grp)} zpravy v {when.astimezone(LOCAL_TZ):%H:%M})")
            if not dry:
                time.sleep(min(wait, 9 * 60))
        mins_now = (when - dt.datetime.now(UTC)).total_seconds() / 60.0
        if discord_send(build_ping_embed(grp, mins_now, source), dry=dry):
            names = ", ".join(e["title"] for e in grp)
            log(f"PING odeslan ({len(grp)}x, za {mins_now:.1f} min): {names}")
            if not dry:
                for e in grp:
                    pinged.add(e["id"])
                changed = True

    if not groups:
        log("Zadna zprava v okne pro ping")

    if changed and not dry:
        st["pinged"] = sorted(pinged)
        save_state(st)
    # signal pro CI, zda se ma stav commitnout
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"state_changed={'true' if changed else 'false'}\n")
    return True


def do_now(dry=False):
    """Okamzity souhrn na vyzadani - pro ikonu na plose."""
    events, source = get_events()
    now = dt.datetime.now(LOCAL_TZ)
    day = now.date()
    rel = gold_relevant(events)
    evs = events_for_day(rel, day)
    emb = build_digest_embed(day, evs, source)
    emb["title"] = "[RUCNI DOTAZ] " + emb["title"]
    # pridej co jeste dnes zbyva
    left = [e for e in evs if e["when"] > dt.datetime.now(UTC)]
    if left:
        nxt = left[0]
        mins = (nxt["when"] - dt.datetime.now(UTC)).total_seconds() / 60.0
        emb.setdefault("fields", []).append({
            "name": "Nejblizsi zprava",
            "value": f"{fmt_line(nxt)}\n-> za **{int(mins)} min**",
        })
    ok = discord_send(emb, dry=dry)
    print("\n" + "=" * 62)
    print(f"  XAUUSD / GOLD - {day.strftime('%d.%m.%Y')}   (zdroj: {source})")
    print("=" * 62)
    if not evs:
        print("  Zadne High ani Medium impact USD zpravy dnes.")
    for e in evs:
        t = e["when"].astimezone(LOCAL_TZ).strftime("%H:%M")
        print(f"  {t}  {e['impact']:<6} {e['currency']:<4} {e['title']}")
    print("=" * 62)
    print("  Odeslano na Discord: " + ("ANO" if ok else "NE - chyba"))
    print("=" * 62 + "\n")
    return ok


# ----------------------------------------------------------------------------
# Selftest
# ----------------------------------------------------------------------------
def do_selftest():
    fails = []

    def check(name, cond, detail=""):
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    print("\n=== SELFTEST ===")

    # 1 timezone
    jan = dt.datetime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(LOCAL_TZ)
    jul = dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(LOCAL_TZ)
    check("TZ zima = UTC+1", jan.utcoffset() == dt.timedelta(hours=1), str(jan.utcoffset()))
    check("TZ leto = UTC+2", jul.utcoffset() == dt.timedelta(hours=2), str(jul.utcoffset()))

    # 2 fallback tz shoda
    fb = _prague_fallback()
    o1 = dt.datetime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(fb).utcoffset()
    o2 = dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(fb).utcoffset()
    check("fallback TZ zima", o1 == dt.timedelta(hours=1), str(o1))
    check("fallback TZ leto", o2 == dt.timedelta(hours=2), str(o2))

    # 3 stabilita ID
    w = dt.datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    check("ID je stabilni", mk_id(w, "ISM PMI", "USD") == mk_id(w, " ism pmi ", "USD"))
    check("ID se lisi dle casu",
          mk_id(w, "X", "USD") != mk_id(w + dt.timedelta(minutes=1), "X", "USD"))

    # 4 filtr
    fake = [
        {"id": "a", "title": "NFP", "currency": "USD", "impact": "High", "when": w},
        {"id": "b", "title": "Small", "currency": "USD", "impact": "Low", "when": w},
        {"id": "c", "title": "JPY thing", "currency": "JPY", "impact": "High", "when": w},
        {"id": "d", "title": "G20", "currency": "All", "impact": "Medium", "when": w},
    ]
    rel = gold_relevant(fake)
    check("filtr propusti USD High + All Medium", len(rel) == 2,
          str(sorted(x["id"] for x in rel)))
    check("filtr odmitne Low a JPY",
          all(x["id"] not in ("b", "c") for x in rel))

    # 5 rozdeleni na den
    d = w.astimezone(LOCAL_TZ).date()
    check("events_for_day", len(events_for_day(rel, d)) == 2)

    # 6 ping okno
    now_u = dt.datetime.now(UTC)
    cases = [(3, True), (5, True), (8, True), (9, False), (13, False), (-2, False), (0, False)]
    for m, want in cases:
        mins = ((now_u + dt.timedelta(minutes=m)) - now_u).total_seconds() / 60.0
        got = 0.0 < mins <= PING_WINDOW_MIN
        check(f"okno {m:+d} min -> {'ping' if want else 'nic'}", got == want)

    # 7 embed rendering
    ev = {"id": "z", "title": "ISM Manufacturing PMI", "currency": "USD",
          "impact": "High", "when": now_u + dt.timedelta(minutes=5),
          "forecast": "48.5", "previous": "48.0", "source": "T"}
    de = build_digest_embed(dt.date(2026, 9, 1), [ev], "T")
    pe = build_ping_embed(ev, 5, "T")
    check("digest embed ma titulek + popis", bool(de["title"]) and bool(de["description"]))
    check("digest obsahuje nazev eventu", "ISM Manufacturing PMI" in de["description"])
    check("ping embed obsahuje 'ZA 5 MIN'", "ZA 5 MIN" in pe["title"], pe["title"])
    ev2 = dict(ev, id="z2", title="JOLTS Job Openings", impact="Medium")
    pg = build_ping_embed([ev, ev2], 5, "T")
    check("skupinovy ping = 1 zprava pro 2 eventy", "2 zpravy" in pg["title"], pg["title"])
    check("skupinovy ping obsahuje oba nazvy",
          "ISM Manufacturing PMI" in pg["description"] and "JOLTS" in pg["description"])
    check("skupinovy ping ma barvu High (je tam High)", pg["color"] == COLOR_HIGH)
    check("jednotlivy ping ma nazev eventu v titulku",
          "ISM Manufacturing PMI" in pe["title"])
    check("prazdny digest je zeleny", build_digest_embed(dt.date(2026, 9, 1), [], "T")["color"] == COLOR_CALM)
    check("embed <= 6000 znaku", len(json.dumps(de)) < 6000)

    # 8 stav round-trip
    global STATE_PATH
    orig = STATE_PATH
    STATE_PATH = os.path.join(HERE, "state.selftest.json")
    try:
        save_state({"digest_sent_for": "2026-09-01", "pinged": ["x", "y"]})
        s = load_state()
        check("state round-trip", s["digest_sent_for"] == "2026-09-01" and "x" in s["pinged"])
        save_state({"digest_sent_for": "d", "pinged": [str(i) for i in range(900)]})
        check("state se omezi na 400 zaznamu", len(load_state()["pinged"]) == 400)
    finally:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        STATE_PATH = orig

    # 9 zive zdroje
    try:
        tv = fetch_tradingview()
        check("TradingView zdroj zije", len(tv) > 0, f"{len(tv)} eventu")
    except Exception as ex:
        check("TradingView zdroj zije", False, str(ex))
    try:
        ff, ffage = fetch_forexfactory()
        check("ForexFactory zdroj zije", len(ff) > 0, f"{len(ff)} eventu")
        check("FF data nejsou prilis stara", ffage <= FF_MAX_STALE_SEC,
              f"{ffage / 3600:.1f}h")
    except Exception as ex:
        check("ForexFactory zdroj zije (nebo cache)", False, str(ex)[:120])
    try:
        ev, src = get_events()
        check("get_events vrati data + zdroj", len(ev) > 0 and bool(src), f"{len(ev)} / {src}")
    except Exception as ex:
        check("get_events vrati data + zdroj", False, str(ex)[:120])

    # 10 webhook nastaven
    check("webhook je nakonfigurovan", bool(webhook_url()))

    print(f"\n=== VYSLEDEK: {'VSE OK' if not fails else 'SELHALO: ' + ', '.join(fails)} ===\n")
    return 0 if not fails else 1


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--digest", action="store_true")
    g.add_argument("--watch", action="store_true")
    g.add_argument("--now", action="store_true")
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-sleep", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return do_selftest()
    if a.digest:
        return 0 if do_digest(dry=a.dry, force=a.force) else 1
    if a.watch:
        return 0 if do_watch(dry=a.dry, allow_sleep=not a.no_sleep) else 1
    if a.now:
        return 0 if do_now(dry=a.dry) else 1
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as ex:
        log(f"FATAL: {type(ex).__name__}: {ex}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
