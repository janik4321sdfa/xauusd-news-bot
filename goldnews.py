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

# Windows konzole/soubor jinak pouzije cp1250 a emoji shodi cely vypis
# (UnicodeEncodeError). Vynutime UTF-8 nezavisle na prostredi.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


def _remember(ev, src):
    _MEMO["at"] = time.time()
    _MEMO["val"] = (ev, src)
    return ev, src


_MEMO = {"at": 0.0, "val": None}
MEMO_TTL = 240      # v --loop rezimu nedotazuj zdroje pri kazdem pruchodu


def get_events(use_memo=False):
    """FF primarne. Kdyz je FF cache prilis stara (nebo FF uplne padne),
    prepne se na TradingView. Nikdy neposila tyden stara data jako aktualni."""
    if use_memo and _MEMO["val"] and (time.time() - _MEMO["at"]) < MEMO_TTL:
        return _MEMO["val"]

    ff, age = None, None
    try:
        ff, age = fetch_forexfactory()
    except Exception as ex:
        log(f"FF: NEDOSTUPNE ({type(ex).__name__}: {ex})")

    if ff and age is not None and age <= FF_MAX_STALE_SEC:
        return _remember(ff, "ForexFactory")

    if ff:
        log(f"FF: cache je {age / 3600:.1f}h stara (limit {FF_MAX_STALE_SEC / 3600:.0f}h)"
            f" -> zkousim TradingView")
    try:
        tv = fetch_tradingview()
        if tv:
            return _remember(tv, "TradingView (zaloha)")
    except Exception as ex:
        log(f"TV: NEDOSTUPNE ({type(ex).__name__}: {ex})")

    if ff:
        log("Oba zdroje problematicke -> pouzivam starou FF cache")
        return _remember(ff, f"ForexFactory (cache {age / 3600:.0f}h)")

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


# ----------------------------------------------------------------------------
# Dopad na ZLATO
#
# Zlato se kotuje v dolarech a konkuruje urokovym vynosum:
#   silna US data -> silnejsi dolar -> ZLATO PADA
#   slaba US data -> slabsi dolar   -> ZLATO ROSTE
#
# "INV" = cim vyssi cislo, tim silnejsi ekonomika (vetsina ukazatelu).
# "DIR" = cim vyssi cislo, tim SLABSI ekonomika (nezamestnanost, zadosti).
#
# POZOR na navrh zprav: pred vydanim se smer NEDA predpovedet. Trh reaguje na
# odchylku skutecneho cisla od PROGNOZY. Proto se uzivateli ukazuje vyhradne
# reakcni pravidlo (od jakeho cisla se to lame), nikoliv dohad o smeru.
# ----------------------------------------------------------------------------

# vyssi cislo = SLABSI ekonomika = zlato roste
GOLD_DIRECT = (
    "unemployment rate", "unemployment claims", "jobless claims",
    "initial claims", "continuing claims", "challenger job cuts",
)

# vyssi cislo = SILNEJSI ekonomika / vyssi inflace = zlato pada
GOLD_INVERSE = (
    "non-farm", "nonfarm", "payroll", "employment change",
    "average hourly earnings", "employment cost", "jolts", "job openings",
    "job quits", "adp",
    "ism", "pmi", "chicago pmi", "philly fed", "empire state",
    "richmond", "dallas fed", "prices paid",
    "cpi", "ppi", "pce", "inflation", "gdp",
    "retail sales", "durable goods", "factory orders",
    "industrial production", "capacity utilization",
    "housing starts", "building permits", "home sales", "house price",
    "consumer confidence", "consumer sentiment", "consumer credit",
    "personal income", "personal spending", "trade balance",
    "productivity", "unit labor costs", "inventories",
    "federal funds rate", "interest rate", "treasury yield",
    "business optimism", "economic optimism",
)


def parse_value(txt):
    """'7.33M' -> 7330000 | '-0.7%' -> -0.7 | '165K' -> 165000 | '' -> None"""
    if not txt:
        return None
    t = str(txt).strip().replace(",", "").replace("<", "").replace(">", "")
    t = t.replace("%", "").strip()
    if not t or t.upper() in ("N/A", "NA", "-", "--"):
        return None
    mult = 1.0
    if t and t[-1] in "KkMmBbTt":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[t[-1].lower()]
        t = t[:-1].strip()
    try:
        return float(t) * mult
    except ValueError:
        return None


def gold_polarity(title):
    """'DIR' | 'INV' | None (nelze urcit)"""
    t = (title or "").lower()
    for k in GOLD_DIRECT:
        if k in t:
            return "DIR"
    for k in GOLD_INVERSE:
        if k in t:
            return "INV"
    return None


UP, DOWN, FLAT, UNK = "\U0001F4C8", "\U0001F4C9", "\u2796", "\u2754"


def gold_view(e):
    """Vraci co se ma zobrazit u jedne zpravy.

    kind   : 'RULE' | 'NOFORECAST' | 'UNKNOWN'
    numbers: radek s cisly (nebo "")
    rule   : seznam radku reakcniho pravidla
    bias   : UP/DOWN/FLAT/None - JEN pro interni pouziti a testy,
             uzivateli se nezobrazuje (byl to zdroj nedorozumeni)
    """
    pol = gold_polarity(e.get("title"))
    fc = (e.get("forecast") or "").strip()
    pv = (e.get("previous") or "").strip()
    f, p = parse_value(fc), parse_value(pv)

    nums = []
    if fc:
        nums.append(f"\u010dek\u00e1 se **{fc}**")
    if pv:
        nums.append(f"minule {pv}")
    numbers = "  \u00b7  ".join(nums)

    # interni bias (prognoza vs minule) - pouze pro testy
    bias = None
    if pol and f is not None and p is not None:
        if f == p:
            bias = FLAT
        else:
            bias = DOWN if ((f > p) if pol == "INV" else (f < p)) else UP

    # udalost bez jednoznacneho dopadu (reci bankeru, jednani, aukce)
    if pol is None:
        msg = (f"{UNK} dopad na zlato nejasn\u00fd" if fc else
               f"{UNK} **bez progn\u00f3zy** \u2014 dopad na zlato nejasn\u00fd")
        return {"kind": "UNKNOWN", "numbers": numbers, "bias": None,
                "rule": [msg], "short": f"{UNK}"}

    # chybi prognoza -> neni od ceho merit odchylku
    if f is None:
        return {"kind": "NOFORECAST", "numbers": numbers, "bias": None,
                "rule": [f"{UNK} **bez progn\u00f3zy** \u2014 sm\u011br se p\u0159edem "
                         f"ur\u010dit ned\u00e1"],
                "short": f"{UNK}"}

    if pol == "INV":
        up_side, down_side = "pod", "nad"
    else:
        up_side, down_side = "nad", "pod"

    return {
        "kind": "RULE", "numbers": numbers, "bias": bias,
        "rule": [f"{UP} {up_side} **{fc}** \u2192 zlato ROSTE",
                 f"{DOWN} {down_side} **{fc}** \u2192 zlato PAD\u00c1"],
        "short": f"{UP}{DOWN} {fc}",
    }


def _card(e, show_time=True, past=False):
    """(nazev, hodnota) pro jednu zpravu jako Discord field.

    past=True -> zprava uz vysla. Reakcni pravidlo se vynechava (je bezpredmetne)
    a cisla se prepnou do minuleho casu, aby to nevypadalo jako predpoved.
    """
    icon = IMPACT_ICON.get(e["impact"], "")
    v = gold_view(e)

    if past:
        fc = (e.get("forecast") or "").strip()
        pv = (e.get("previous") or "").strip()
        bits = []
        if fc:
            bits.append(f"\u010dekalo se {fc}")
        if pv:
            bits.append(f"předtím {pv}")
        body = ["  \u00b7  ".join(bits) if bits else "\u2014"]
        mark = "\u2714\ufe0f"
    else:
        body = ([v["numbers"]] if v["numbers"] else []) + v["rule"]
        mark = icon

    if show_time:
        t = e["when"].astimezone(LOCAL_TZ).strftime("%H:%M")
        name = f"{mark}  {t}  \u00b7  {e['title']}"
    else:
        name = f"{mark}  {e['title']}"
    if past:
        name += "   (u\u017e vy\u0161lo)"
    return name, "\n".join(body)


def fmt_event_compact(e):
    t = e["when"].astimezone(LOCAL_TZ).strftime("%H:%M")
    icon = IMPACT_ICON.get(e["impact"], "")
    v = gold_view(e)
    tail = f"  \u2014  {v['numbers']}" if v["numbers"] else ""
    return f"{icon} **{t}**  {e['title']}{tail}"


fmt_event = fmt_event_compact
fmt_line = fmt_event_compact

HINT = ("\u0160ipky plat\u00ed proti **progn\u00f3ze**, ne proti minul\u00e9 hodnot\u011b. "
        "Trh reaguje na odchylku od progn\u00f3zy.")


DNY = ("po", "ut", "st", "ct", "pa", "so", "ne")


def _human_wait(mins):
    """65 -> 'za 1 h 5 min' | 1500 -> 'za 1 d 1 h'"""
    mins = max(0, int(round(mins)))
    if mins < 60:
        return f"za {mins} min"
    if mins < 24 * 60:
        return f"za {mins // 60} h {mins % 60} min"
    d, rest = divmod(mins, 24 * 60)
    return f"za {d} d {rest // 60} h"


def _when_label(e, today):
    """'16:00' pro dnes, 'st 02.09 14:15' pro jiny den."""
    d = e["when"].astimezone(LOCAL_TZ)
    if d.date() == today:
        return d.strftime("%H:%M")
    return f"{DNY[d.weekday()]} {d.strftime('%d.%m %H:%M')}"


def _sorted_evs(evs):
    return sorted(evs, key=lambda x: (x["when"], x["impact"] != "High", x["title"]))


def build_digest_embed(day, evs, source, now=None):
    ds = day.strftime("%d.%m.%Y")
    now = now or dt.datetime.now(UTC)
    if not evs:
        return {
            "author": {"name": "XAUUSD \u00b7 GOLD"},
            "title": f"\u2705 {ds} \u2014 \u017e\u00e1dn\u00e9 v\u00fdznamn\u00e9 zpr\u00e1vy",
            "description": "Dnes nevych\u00e1z\u00ed \u017e\u00e1dn\u00e1 High ani Medium "
                           "zpr\u00e1va pro dolar. Klidn\u00fd den pro zlato.",
            "color": COLOR_CALM,
            "footer": {"text": foot(source)},
        }

    evs = _sorted_evs(evs)
    nh = sum(1 for e in evs if e["impact"] == "High")
    nm = len(evs) - nh
    npast = sum(1 for e in evs if e["when"] <= now)
    head = []
    if nh:
        head.append(f"{IMPACT_ICON['High']} **{nh}\u00d7 High**")
    if nm:
        head.append(f"{IMPACT_ICON['Medium']} **{nm}\u00d7 Medium**")
    if npast:
        head.append("\u00b7  " + ("v\u0161echny u\u017e vy\u0161ly"
                                   if npast == len(evs)
                                   else f"{npast} u\u017e vy\u0161lo"))

    emb = {
        "author": {"name": "XAUUSD \u00b7 GOLD"},
        "title": f"\U0001F4C5 Dne\u0161n\u00ed zpr\u00e1vy \u2014 {ds}",
        "description": "     ".join(head),
        "color": COLOR_HIGH if nh else COLOR_MED,
        "timestamp": dt.datetime.now(UTC).isoformat(),
        "footer": {"text": foot(source) + " \u00b7 \u010dasy Praha"},
    }

    if len(evs) <= 20:
        fields = []
        for e in evs:
            n, v = _card(e, past=e["when"] <= now)
            fields.append({"name": n, "value": v, "inline": False})
        # vysvetlivka jen kdyz je co vysvetlovat (nejaka zprava jeste nevysla)
        if npast < len(evs):
            fields.append({"name": "\u2139\ufe0f  Jak to \u010d\u00edst",
                           "value": HINT, "inline": False})
        emb["fields"] = fields
    else:
        emb["description"] += "\n\n" + "\n".join(fmt_event_compact(e) for e in evs)
        emb["fields"] = [{"name": "\u2139\ufe0f", "value": HINT}]
    return emb


def build_ping_embed(evs, minutes, source):
    """evs = zpravy se STEJNYM casem -> jedna zprava misto nekolika."""
    if not isinstance(evs, list):
        evs = [evs]
    evs = _sorted_evs(evs)
    when = evs[0]["when"]
    t = when.astimezone(LOCAL_TZ).strftime("%H:%M")
    has_high = any(e["impact"] == "High" for e in evs)
    icon = IMPACT_ICON["High"] if has_high else IMPACT_ICON["Medium"]
    m = max(0, int(round(minutes)))

    emb = {
        "author": {"name": f"XAUUSD \u00b7 za {m} min"},
        "color": COLOR_HIGH if has_high else COLOR_MED,
        "timestamp": when.isoformat(),
        "footer": {"text": foot(source)},
    }

    if len(evs) == 1:
        e = evs[0]
        v = gold_view(e)
        emb["title"] = f"{icon}  {e['title']}"
        lines = [f"**{e['impact']}**  \u00b7  vych\u00e1z\u00ed v **{t}**", ""]
        if v["numbers"]:
            lines.append(v["numbers"])
        lines += v["rule"]
        emb["description"] = "\n".join(lines)
    else:
        emb["title"] = f"{icon}  {len(evs)} zpr\u00e1vy v {t}"
        emb["description"] = f"V\u0161echny vych\u00e1zej\u00ed v **{t}**."
        emb["fields"] = [{"name": n, "value": v, "inline": False}
                         for n, v in (_card(e, show_time=False) for e in evs)]
    return emb


# ----------------------------------------------------------------------------
# Ulozeni stavu do gitu (jen v CI, kdyz GOLDNEWS_GIT_SYNC=1).
# Diky tomu se pri restartu jobu neposle uz odeslany ping znovu.
# ----------------------------------------------------------------------------
def git_sync(msg="chore: state update [skip ci]"):
    if os.environ.get("GOLDNEWS_GIT_SYNC") != "1":
        return
    import subprocess
    try:
        d = os.path.dirname(STATE_PATH) or "."
        chk = subprocess.run(["git", "status", "--porcelain",
                              "state.json", "ff_cache.json"],
                             cwd=d, capture_output=True, text=True, timeout=60)
        if not (chk.stdout or "").strip():
            return
        for cmd in (["git", "add", "state.json", "ff_cache.json"],
                    ["git", "commit", "-m", msg]):
            subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=60)
        for _ in range(3):
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"],
                           cwd=d, capture_output=True, text=True, timeout=120)
            pr = subprocess.run(["git", "push", "origin", "HEAD:main"],
                                cwd=d, capture_output=True, text=True, timeout=120)
            if pr.returncode == 0:
                log("stav ulozen do gitu")
                return
            time.sleep(4)
        log("stav se nepodarilo ulozit do gitu")
    except Exception as ex:
        log(f"git_sync chyba: {type(ex).__name__}: {ex}")


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


def do_watch(dry=False, allow_sleep=True, use_memo=False):
    """Jeden hlidaci pruchod: denni souhrn (je-li cas) + pingy 5 min predem."""
    events, source = get_events(use_memo=use_memo)
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
        git_sync()
    # signal pro CI, zda se ma stav commitnout
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"state_changed={'true' if changed else 'false'}\n")
    return True


def do_loop(minutes=330, interval=45, dry=False):
    """Dlouhobezici hlidac. Nezavisly na presnosti cronu - kontroluje kazdych
    `interval` sekund, takze ping odejde presne na T-5min."""
    end = time.time() + minutes * 60.0
    log(f"LOOP start: bezi {minutes} min, kontrola kazdych {interval}s")
    n = 0
    while time.time() < end:
        n += 1
        try:
            do_watch(dry=dry, allow_sleep=True, use_memo=True)
        except Exception as ex:
            log(f"LOOP pruchod {n} chyba: {type(ex).__name__}: {ex}")
        left = end - time.time()
        if left <= 0:
            break
        time.sleep(max(1.0, min(interval, left)))
    log(f"LOOP konec po {n} pruchodech")
    return True


def do_now(dry=False):
    """Okamzity souhrn na vyzadani - pro ikonu na plose."""
    events, source = get_events()
    now = dt.datetime.now(LOCAL_TZ)
    day = now.date()
    rel = gold_relevant(events)
    evs = events_for_day(rel, day)
    emb = build_digest_embed(day, evs, source)
    emb["author"] = {"name": "XAUUSD \u00b7 GOLD \u00b7 ru\u010dn\u00ed dotaz"}

    # nejblizsi zprava - napric dny, ne jen dnes (jinak po 16:00 nic nerekne)
    now_u = dt.datetime.now(UTC)
    left = sorted([e for e in rel if e["when"] > now_u], key=lambda x: x["when"])
    if left:
        nxt = left[0]
        mins = (nxt["when"] - now_u).total_seconds() / 60.0
        icon = IMPACT_ICON.get(nxt["impact"], "")
        v = gold_view(nxt)
        val = [f"{icon} **{_when_label(nxt, day)}**  \u00b7  {nxt['title']}",
               f"**{_human_wait(mins)}**"]
        if v["numbers"]:
            val.append(v["numbers"])
        val += v["rule"]
        emb.setdefault("fields", []).append({
            "name": "\u23ed\ufe0f  Nejbli\u017e\u0161\u00ed zpr\u00e1va",
            "value": "\n".join(val), "inline": False})
    else:
        emb.setdefault("fields", []).append({
            "name": "\u23ed\ufe0f  Nejbli\u017e\u0161\u00ed zpr\u00e1va",
            "value": "V na\u010dten\u00e9m kalend\u00e1\u0159i u\u017e "
                     "\u017e\u00e1dn\u00e1 dal\u0161\u00ed High/Medium "
                     "zpr\u00e1va nen\u00ed.", "inline": False})
    ok = discord_send(emb, dry=dry)
    print("\n" + "=" * 62)
    print(f"  XAUUSD / GOLD - {day.strftime('%d.%m.%Y')}   (zdroj: {source})")
    print("=" * 62)
    if not evs:
        print("  Zadne High ani Medium impact USD zpravy dnes.")
    now_u2 = dt.datetime.now(UTC)
    for e in evs:
        t = e["when"].astimezone(LOCAL_TZ).strftime("%H:%M")
        v = gold_view(e)
        fc = (e.get("forecast") or "").strip()
        if e["when"] <= now_u2:
            hint = "UZ VYSLO"
        elif v["kind"] == "RULE":
            pol = gold_polarity(e["title"])
            hint = (f"pod {fc} = roste / nad {fc} = pada" if pol == "INV"
                    else f"nad {fc} = roste / pod {fc} = pada")
        elif v["kind"] == "NOFORECAST":
            hint = "bez prognozy"
        else:
            hint = "dopad nejasny"
        print(f"  {t}  {e['impact']:<6} {e['title'][:34]:<34}  {hint}")
    if left:
        nx = left[0]
        print("-" * 62)
        print(f"  DALSI: {_when_label(nx, day)}  {nx['impact']:<6} {nx['title'][:34]}"
              f"   ({_human_wait((nx['when'] - now_u2).total_seconds() / 60.0)})")
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

    def _alltext(emb):
        """Vsechen text z embedu - testy tak neresi, jestli je to v description
        nebo ve fields."""
        parts = [emb.get("title", ""), emb.get("description", "") or "",
                 (emb.get("author") or {}).get("name", ""),
                 (emb.get("footer") or {}).get("text", "")]
        for f in emb.get("fields", []):
            parts += [f.get("name", ""), f.get("value", "")]
        return "\n".join(parts)

    # 7 embed rendering
    ev = {"id": "z", "title": "ISM Manufacturing PMI", "currency": "USD",
          "impact": "High", "when": dt.datetime.now(UTC) + dt.timedelta(minutes=5),
          "forecast": "48.5", "previous": "48.0", "source": "T"}
    de = build_digest_embed(dt.date(2026, 9, 1), [ev], "T")
    pe = build_ping_embed(ev, 5, "T")
    check("digest ma titulek", bool(de["title"]))
    check("digest obsahuje nazev zpravy", "ISM Manufacturing PMI" in _alltext(de))
    check("digest ma autora", bool((de.get("author") or {}).get("name")))
    check("ping ma nazev zpravy v titulku", "ISM Manufacturing PMI" in pe["title"])
    check("ping ma 'za 5 min' v autorovi", "za 5 min" in _alltext(pe), _alltext(pe)[:60])

    ev2 = dict(ev, id="z2", title="JOLTS Job Openings", impact="Medium")
    pg = build_ping_embed([ev, ev2], 5, "T")
    check("skupinovy ping = 1 zprava pro 2 zpravy", "2 zpr" in pg["title"], pg["title"])
    txt = _alltext(pg)
    check("skupinovy ping obsahuje oba nazvy",
          "ISM Manufacturing PMI" in txt and "JOLTS" in txt)
    check("skupinovy ping ma barvu High", pg["color"] == COLOR_HIGH)
    check("prazdny digest je zeleny",
          build_digest_embed(dt.date(2026, 9, 1), [], "T")["color"] == COLOR_CALM)
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

    # 9b memo cache
    _MEMO["at"] = 0.0
    _MEMO["val"] = None
    e1 = get_events(use_memo=True)
    t0 = time.time()
    e2 = get_events(use_memo=True)
    check("memo cache funguje (2. dotaz je okamzity)", (time.time() - t0) < 0.25)
    check("memo vraci stejna data", e1[1] == e2[1])

    # 9c do_loop existuje a ma spravnou signaturu
    import inspect
    check("do_loop existuje", callable(do_loop))
    check("do_loop ma parametry minutes/interval",
          {"minutes", "interval"} <= set(inspect.signature(do_loop).parameters))
    check("git_sync je bez GOLDNEWS_GIT_SYNC no-op",
          (os.environ.get("GOLDNEWS_GIT_SYNC") != "1") and (git_sync() is None))

    # 9d vypis emoji nesmi shodit proces ani pri presmerovani do souboru
    try:
        print("  [emoji test] " + IMPACT_ICON["High"] + IMPACT_ICON["Medium"]
              + IMPACT_ICON["Low"])
        check("emoji lze vypsat (UTF-8 vynuceno)", True)
    except UnicodeEncodeError as ex:
        check("emoji lze vypsat (UTF-8 vynuceno)", False, str(ex))

    # 9e parsovani cisel
    for txt_, want in (("55.2", 55.2), ("-0.7%", -0.7), ("165K", 165000.0),
                       ("7.33M", 7330000.0), ("1.2B", 1.2e9), ("3,500", 3500.0),
                       ("0.0%", 0.0), ("", None), ("N/A", None), ("<0.1%", 0.1)):
        got = parse_value(txt_)
        check(f"parse_value({txt_!r}) = {want}", got == want, str(got))

    # 9f zarazeni ukazatelu (INV = vyssi cislo znamena silnejsi ekonomiku)
    for title, want in [
        ("ISM Manufacturing PMI", "INV"),
        ("Non-Farm Employment Change", "INV"),
        ("ADP Non-Farm Employment Change", "INV"),
        ("Average Hourly Earnings m/m", "INV"),
        ("Core CPI m/m", "INV"),
        ("JOLTS Job Openings", "INV"),
        ("Federal Funds Rate", "INV"),
        ("Unemployment Rate", "DIR"),
        ("Unemployment Claims", "DIR"),
        ("Continuing Jobless Claims", "DIR"),
        ("Fed Barr Speech", None),
        ("G20 Meetings", None),
        ("52-Week Bill Auction", None),
    ]:
        check(f"polarita '{title}' = {want}", gold_polarity(title) == want,
              str(gold_polarity(title)))

    def _ev(title, fc, pv, imp="High"):
        return {"id": "x", "title": title, "currency": "USD", "impact": imp,
                "when": dt.datetime.now(UTC) + dt.timedelta(minutes=30),
                "forecast": fc, "previous": pv, "source": "T"}

    # 9g reakcni pravidlo - JEDINA vec, kterou uzivatel vidi.
    #    INV: pod prognozu = zlato roste. DIR: nad prognozu = zlato roste.
    inv = gold_view(_ev("ISM Manufacturing PMI", "55.2", "55.6"))
    check("INV ma pravidlo o 2 radcich", len(inv["rule"]) == 2)
    check("INV: 'pod' je na radku s ROSTE",
          "pod" in inv["rule"][0] and "ROSTE" in inv["rule"][0], inv["rule"][0])
    check("INV: 'nad' je na radku s PADA",
          "nad" in inv["rule"][1] and "PAD" in inv["rule"][1], inv["rule"][1])
    check("INV: pravidlo obsahuje prognozu", "55.2" in inv["rule"][0])

    dr = gold_view(_ev("Unemployment Claims", "240K", "220K"))
    check("DIR: 'nad' je na radku s ROSTE",
          "nad" in dr["rule"][0] and "ROSTE" in dr["rule"][0], dr["rule"][0])
    check("DIR: 'pod' je na radku s PADA",
          "pod" in dr["rule"][1] and "PAD" in dr["rule"][1], dr["rule"][1])

    # 9h chybejici prognoza se VZDY rekne
    for t_, f_, p_ in (("Fed Chair Powell Speaks", "", ""),
                       ("ISM Manufacturing PMI", "", "55.6"),
                       ("G20 Meetings", "", "")):
        vx = gold_view(_ev(t_, f_, p_))
        txt = " ".join(vx["rule"]).lower()
        check(f"'{t_}' -> vyslovne rekne, ze prognoza chybi",
              "bez progn" in txt, txt[:70])
        check(f"'{t_}' -> zadne reakcni pravidlo (neni od ceho merit)",
              "ROSTE" not in " ".join(vx["rule"]))

    # 9i interni bias (uzivateli se NEZOBRAZUJE, ale logika musi byt spravna)
    for title, fc, pv, want, why in [
        ("ISM Manufacturing PMI", "55.2", "55.6", UP, "slabsi PMI = zlato nahoru"),
        ("ISM Manufacturing PMI", "56.0", "55.6", DOWN, "silnejsi PMI = zlato dolu"),
        ("Non-Farm Employment Change", "165K", "142K", DOWN, "vic mist = zlato dolu"),
        ("Core CPI m/m", "0.4%", "0.2%", DOWN, "vyssi inflace = zlato dolu"),
        ("Unemployment Rate", "4.5%", "4.2%", UP, "vyssi nezamestnanost = nahoru"),
        ("Unemployment Claims", "240K", "220K", UP, "vic zadosti = nahoru"),
        ("JOLTS Job Openings", "7.33M", "7.36M", UP, "mene volnych mist = nahoru"),
        ("ISM Manufacturing PMI", "55.6", "55.6", FLAT, "stejne = neutralne"),
    ]:
        check(f"bias: {why}", gold_view(_ev(title, fc, pv))["bias"] == want,
              str(gold_view(_ev(title, fc, pv))["bias"]))

    check("bias se uzivateli nezobrazuje (neni v textu embedu)",
          "CEKA SE" not in _alltext(build_digest_embed(
              dt.date(2026, 9, 1), [_ev("ISM Manufacturing PMI", "55.2", "55.6")], "T")))

    # 9j vzhled a limity
    one = build_digest_embed(dt.date(2026, 9, 1),
                            [_ev("ISM Manufacturing PMI", "55.2", "55.6")], "T")
    t1 = _alltext(one)
    check("digest ma kartu se casem a nazvem", "ISM Manufacturing PMI" in t1)
    check("digest ma reakcni pravidlo", "ROSTE" in t1 and "PAD" in t1)
    check("digest ma vysvetlivku o prognoze", "progn" in t1.lower())
    check("digest ma pocty High/Medium", "High" in (one.get("description") or ""))

    many = [_ev(f"Indicator {i}", "1.0", "0.5",
                "High" if i % 3 == 0 else "Medium") for i in range(15)]
    big = build_digest_embed(dt.date(2026, 9, 1), many, "T")
    check("15 zprav: pouzije karty", len(big.get("fields", [])) == 16,
          str(len(big.get("fields", []))))
    check("15 zprav: pod limitem 6000", len(json.dumps(big)) < 6000,
          str(len(json.dumps(big))))
    check("15 zprav: max 25 fieldu", len(big.get("fields", [])) <= 25)

    huge = [_ev(f"Indicator {i}", "1.0", "0.5") for i in range(30)]
    bigger = build_digest_embed(dt.date(2026, 9, 1), huge, "T")
    check("30 zprav: prepne na kompaktni vypis",
          len(bigger.get("fields", [])) <= 2)
    check("30 zprav: description pod 4096",
          len(bigger.get("description", "")) <= 4096,
          str(len(bigger.get("description", ""))))
    check("30 zprav: pod limitem 6000", len(json.dumps(bigger)) < 6000)

    pg2 = build_ping_embed([_ev("ISM Manufacturing PMI", "55.2", "55.6"),
                            _ev("JOLTS Job Openings", "7.33M", "7.36M", "Medium")],
                           5, "T")
    check("skupinovy ping ma kartu pro kazdou zpravu",
          len(pg2.get("fields", [])) == 2)
    check("skupinovy ping neopakuje cas u kazde karty",
          all(":" not in f["name"].split("\u00b7")[0] for f in pg2["fields"]),
          pg2["fields"][0]["name"])
    check("skupinovy ping ma cas v hlavicce", ":" in pg2["description"])
    check("skupinovy ping pod limitem", len(json.dumps(pg2)) < 6000)

    solo = build_ping_embed([_ev("ISM Manufacturing PMI", "55.2", "55.6")], 5, "T")
    check("jedna zprava: bez fieldu, vse v popisu", not solo.get("fields"))
    check("jedna zprava: ma cisla i pravidlo",
          "55.2" in solo["description"] and "ROSTE" in solo["description"])

    check("diakritika prosla do textu", "\u010dek\u00e1 se" in t1, t1[:40])

    # 9k uz vydane zpravy se nesmi tvarit jako predpoved
    past_ev = _ev("ISM Manufacturing PMI", "55.2", "55.6")
    past_ev["when"] = dt.datetime.now(UTC) - dt.timedelta(hours=2)
    fut_ev = _ev("JOLTS Job Openings", "7.33M", "7.36M", "Medium")
    fut_ev["when"] = dt.datetime.now(UTC) + dt.timedelta(hours=2)

    n_p, v_p = _card(past_ev, past=True)
    check("minula zprava ma znacku 'uz vyslo'", "vy\u0161lo" in n_p, n_p)
    check("minula zprava NEMA reakcni pravidlo",
          "ROSTE" not in v_p and "PAD" not in v_p, v_p)
    check("minula zprava ma minuly cas", "\u010dekalo se" in v_p, v_p)

    n_f, v_f = _card(fut_ev, past=False)
    check("budouci zprava MA reakcni pravidlo", "ROSTE" in v_f)
    check("budouci zprava ma pritomny cas", "\u010dek\u00e1 se" in v_f)
    check("budouci zprava nema znacku 'uz vyslo'", "vy\u0161lo" not in n_f)

    # digest: mix minulosti a budoucnosti
    mix = build_digest_embed(dt.date.today(), [past_ev, fut_ev], "T")
    tm = _alltext(mix)
    check("digest oznaci, kolik uz vyslo", "u\u017e vy\u0161lo" in tm, tm[:160])
    check("digest s budoucim eventem ma vysvetlivku",
          any("Jak to" in f["name"] for f in mix["fields"]))

    allpast = build_digest_embed(dt.date.today(), [past_ev], "T")
    ta = _alltext(allpast)
    check("kdyz vsechno vyslo, rekne to", "v\u0161echny u\u017e vy\u0161ly" in ta,
          (allpast.get("description") or "")[:120])
    check("kdyz vsechno vyslo, vysvetlivka je zbytecna -> chybi",
          not any("Jak to" in f["name"] for f in allpast.get("fields", [])))
    check("vysvetlivka ma nadpis, ne jen emoji",
          all(len(f["name"].strip()) > 3 for f in mix["fields"]))

    # 9l lidsky cas cekani
    for m, want in ((0, "za 0 min"), (45, "za 45 min"), (65, "za 1 h 5 min"),
                    (1500, "za 1 d 1 h")):
        check(f"_human_wait({m}) = {want}", _human_wait(m) == want, _human_wait(m))

    # 9m popisek casu
    today_ = dt.datetime.now(LOCAL_TZ).date()
    e_dnes = _ev("X", "1", "1")
    e_dnes["when"] = dt.datetime.combine(today_, dt.time(16, 0),
                                         LOCAL_TZ).astimezone(UTC)
    check("dnesni zprava = jen cas", _when_label(e_dnes, today_) == "16:00",
          _when_label(e_dnes, today_))
    e_jinak = _ev("Y", "1", "1")
    e_jinak["when"] = (dt.datetime.combine(today_, dt.time(14, 15), LOCAL_TZ)
                       + dt.timedelta(days=2)).astimezone(UTC)
    lbl = _when_label(e_jinak, today_)
    check("jiny den = zkratka dne + datum", len(lbl) > 6 and "14:15" in lbl, lbl)

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
    g.add_argument("--loop", action="store_true")
    ap.add_argument("--minutes", type=int, default=330)
    ap.add_argument("--interval", type=int, default=45)
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
    if a.loop:
        return 0 if do_loop(minutes=a.minutes, interval=a.interval, dry=a.dry) else 1
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
