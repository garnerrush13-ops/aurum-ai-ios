# -*- coding: utf-8 -*-
"""
XAU/USD Signal Analyzer v3 — Pyto / iPhone
================================================
تحليل فقط — التطبيق لا يفتح أي صفقة تلقائياً.
- Multi-timeframe: H1 + M15 + M5 (H1 يُجلب من API أو يُركَّب من M15)
- Real MACD (12,26,9) used as a signal gate and in the score
- Automatic support / resistance (swing pivots, clustered)
- FVG + IFVG + Liquidity Sweep
- Retest confirmation required before any signal
- Signal-only mode; no order execution
- Signal notifications show entry + two targets only
- No external dependencies (works in Pyto)
"""

import json, os, re, time, math, threading, webbrowser, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", "8089"))
APP_NAME = "Aurum"      # اسم التطبيق - غيّره كما تشاء
API_BASE  = "https://biquote.io/api/XAUUSD/ohlc"
QUOTE_URL = "https://biquote.io/api/XAUUSD"
try:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _BASE_DIR = os.getcwd()
STATE_FILE = os.path.join(_BASE_DIR, "fvgs_state.json")
CACHE_TTL = 5
QUOTE_TTL = 1
MAX_FVGS = 200
SCAN_BACK = 20          # how many recent candles to scan for FVGs on each analysis

# ─────────────────────────── helpers ────────────────────────────
def now_ts(): return time.time()

def safe_float(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d

def log(*a):
    print("[fvg]", *a)

# ─────────────────────── HTTP fetch w/ retry ────────────────────
def _http_get_json(url, timeout=12, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Pyto-FVG-v2"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {last}")

def _parse_time(stamp):
    """ISO string or epoch (seconds / milliseconds) -> epoch seconds (UTC)."""
    if isinstance(stamp, (int, float)):
        v = float(stamp)
        return v / 1000.0 if v > 1e11 else v
    s = str(stamp).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    s = s.replace("Z", "+00:00")
    # normalise fractional seconds to 6 digits (fromisoformat is strict on older Pythons)
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()

_CANDLE_CACHE = {}   # { "5m": (ts, [candles]) }
_CACHE_LOCK = threading.Lock()

def fetch_candles(interval, limit=400, use_cache=True):
    if interval not in ("5m", "15m", "1h"):
        raise ValueError("Unsupported timeframe")
    with _CACHE_LOCK:
        if use_cache and interval in _CANDLE_CACHE:
            ts, data = _CANDLE_CACHE[interval]
            if now_ts() - ts < CACHE_TTL:
                return data
    url = API_BASE + "?" + urllib.parse.urlencode({"interval": interval, "limit": limit})
    try:
        data = _http_get_json(url, timeout=20, retries=2)
        bars = data.get("bars", []) if isinstance(data, dict) else data
        if not isinstance(bars, list):
            raise RuntimeError("Unexpected response format")
        out = []
        for bar in bars:
            if not isinstance(bar, dict) or bar.get("isOpen") is True: continue
            stamp = bar.get("openTime")
            if stamp is None or stamp == "": continue
            try:
                o, h, l, c = (safe_float(bar["open"]), safe_float(bar["high"]),
                              safe_float(bar["low"]), safe_float(bar["close"]))
                if min(o, h, l, c) <= 0 or h < l: continue      # drop broken bars
                out.append({"time": _parse_time(stamp), "open": o, "high": h, "low": l, "close": c})
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        out.sort(key=lambda x: x["time"])
        if len(out) < 60:
            raise RuntimeError("Not enough closed candles")
        with _CACHE_LOCK:
            _CANDLE_CACHE[interval] = (now_ts(), out)
        return out
    except Exception as e:
        with _CACHE_LOCK:
            if interval in _CANDLE_CACHE:
                log("fetch failed, using stale cache:", e)
                return _CANDLE_CACHE[interval][1]
        raise

_QUOTE_CACHE = {"ts": 0.0, "data": None}

def fetch_live_quote():
    """Short cache so several browser polls don't each hit the upstream API."""
    with _CACHE_LOCK:
        if _QUOTE_CACHE["data"] and now_ts() - _QUOTE_CACHE["ts"] < QUOTE_TTL:
            return _QUOTE_CACHE["data"]
    quote = _fetch_live_quote_raw()
    with _CACHE_LOCK:
        _QUOTE_CACHE["ts"], _QUOTE_CACHE["data"] = now_ts(), quote
    return quote

def _fetch_live_quote_raw():
    try:
        data = _http_get_json(QUOTE_URL, timeout=8, retries=1)
        if not isinstance(data, dict):
            raise RuntimeError("Invalid quote response")
        return {
            "mid": safe_float(data.get("mid")),
            "bid": safe_float(data.get("bid")),
            "ask": safe_float(data.get("ask")),
            "spread": safe_float(data.get("spread")),
            "timestamp": data.get("timestamp", ""),
            "marketState": data.get("marketState", "unknown"),
            "stale": bool(data.get("stale", False)),
        }
    except Exception as e:
        log("quote failed:", e)
        raise

def send_telegram(token, chat_id, text):
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            result = json.loads(e.read().decode("utf-8"))   # Telegram explains the error here
        except Exception:
            raise RuntimeError("Telegram HTTP %s" % e.code)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram error"))
    return result

# ───────────────────────── indicators ───────────────────────────
def sma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period

def trend_from(candles, fast_p=20, slow_p=50):
    closes = [c["close"] for c in candles]
    f, s = sma(closes, fast_p), sma(closes, slow_p)
    if f is None or s is None: return "UNKNOWN"
    if closes[-1] > f and f > s: return "BULLISH"
    if closes[-1] < f and f < s: return "BEARISH"
    return "NEUTRAL"

# ───────────────────────── sessions (local city clocks / DST aware) ──
def _city_session_open(ts, zone_name, start_hour, end_hour):
    """Check a session using the financial centre's own timezone and DST."""
    if ZoneInfo is None:
        return False
    try:
        local = datetime.fromtimestamp(ts, ZoneInfo(zone_name))
        if local.weekday() >= 5:
            return False
        hour = local.hour + local.minute / 60.0
        if start_hour < end_hour:
            return start_hour <= hour < end_hour
        return hour >= start_hour or hour < end_hour
    except Exception:
        return False

def session_of(ts):
    # Conventional local business hours; IANA zones adjust daylight saving automatically.
    london = _city_session_open(ts, "Europe/London", 8, 17)
    newyork = _city_session_open(ts, "America/New_York", 8, 17)
    tokyo = _city_session_open(ts, "Asia/Tokyo", 9, 18)
    sydney = _city_session_open(ts, "Australia/Sydney", 7, 16)
    if london and newyork:
        return "overlap"
    if london:
        return "london"
    if newyork:
        return "newyork"
    if tokyo or sydney:
        return "asia"
    return "asia"

# ───────────────────────── FVG engine ───────────────────────────
_state_lock = threading.RLock()
_fvgs = []           # list of FVG dicts
_last_save = 0.0

_REQUIRED_KEYS = ("id", "tf", "type", "top", "bottom", "mid", "size", "atr_at_form",
                  "formed_at", "middle_candle", "status", "session")

def _valid_fvg(f):
    return (isinstance(f, dict) and all(k in f for k in _REQUIRED_KEYS)
            and isinstance(f["middle_candle"], dict)
            and all(k in f["middle_candle"] for k in ("open", "high", "low", "close")))

def load_state():
    global _fvgs
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("fvgs"), list):
            good = [f for f in data["fvgs"] if _valid_fvg(f)]
            for f in good:
                f.setdefault("score", 0); f.setdefault("label", "low"); f.setdefault("score_detail", {})
            good.sort(key=lambda x: x["formed_at"], reverse=True)
            _fvgs = good[:MAX_FVGS]
            log(f"loaded {len(_fvgs)} FVGs from disk")
    except Exception as e:
        log("state load failed:", e)

def persist_state(force=False):
    global _last_save
    if not force and now_ts() - _last_save < 10: return
    _last_save = now_ts()
    try:
        with _state_lock:
            snapshot = json.dumps({"fvgs": _fvgs[:MAX_FVGS], "saved_at": now_ts()})
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot)
        os.replace(tmp, STATE_FILE)          # atomic: never leaves a half-written file
    except Exception as e:
        log("state save failed:", e)

def _fvg_id(tf, formed_at, kind):
    return f"{tf}_{int(formed_at)}_{kind}"

def _make_fvg(tf, kind, top, bottom, c2, c3, atr_val):
    return {
        "id": _fvg_id(tf, c3["time"], kind),
        "tf": tf, "type": "bullish" if kind == "bull" else "bearish",
        "top": top, "bottom": bottom, "mid": (top + bottom) / 2,
        "size": top - bottom, "atr_at_form": atr_val,
        "formed_at": c3["time"],
        "middle_candle": {k: c2[k] for k in ("open", "high", "low", "close")},
        "status": "active", "score": 0, "label": "low",
        "score_detail": {}, "session": session_of(c3["time"]),
    }

def detect_new_fvgs(candles, tf, atr_val, scan_back=1):
    """Scan the last `scan_back` closed candles (as 3-candle windows) for FVGs."""
    out = []
    n = len(candles)
    for end in range(max(3, n - scan_back + 1), n + 1):
        c1, c2, c3 = candles[end - 3], candles[end - 2], candles[end - 1]
        if c3["low"] > c1["high"]:       # bullish: gap between c1.high and c3.low
            out.append(_make_fvg(tf, "bull", c3["low"], c1["high"], c2, c3, atr_val))
        if c3["high"] < c1["low"]:       # bearish: gap between c3.high and c1.low
            out.append(_make_fvg(tf, "bear", c1["low"], c3["high"], c2, c3, atr_val))
    return out

def update_fvg_status(fvg, candles):
    """Update mitigation state based on candles after formation."""
    if fvg["status"] in ("filled", "ifvg"): return fvg["status"]
    recent = [c for c in candles if c["time"] > fvg["formed_at"]]
    if not recent: return fvg["status"]
    top, bot, mid = fvg["top"], fvg["bottom"], fvg["mid"]
    status = fvg["status"]
    for c in recent:
        if fvg["type"] == "bullish":
            if c["close"] < bot:
                fvg["ifvg_at"] = c["time"]; return "ifvg"
            if c["low"] <= bot: return "filled"
            if c["low"] <= mid and status == "active": status = "partial"
        else:
            if c["close"] > top:
                fvg["ifvg_at"] = c["time"]; return "ifvg"
            if c["high"] >= top: return "filled"
            if c["high"] >= mid and status == "active": status = "partial"
    return status

def compute_gold_score(fvg, trend_m15, trend_m5):
    score, detail = 0, {}
    # 1) HTF alignment (25)
    aligned = ((fvg["type"] == "bullish" and trend_m15 == "BULLISH") or
               (fvg["type"] == "bearish" and trend_m15 == "BEARISH"))
    detail["htf"] = 25 if aligned else 0
    score += detail["htf"]
    # 2) Size vs ATR (25)
    ratio = fvg["size"] / fvg["atr_at_form"] if fvg["atr_at_form"] > 0 else 0
    if ratio >= 1.0: pts = 25
    elif ratio >= 0.5: pts = 18
    elif ratio >= 0.25: pts = 10
    else: pts = 0
    detail["size"] = pts
    detail["size_ratio"] = round(ratio, 2)
    score += pts
    # 3) Session (20)
    sess_pts = {"overlap": 20, "london": 18, "newyork": 16, "asia": 5}
    pts = sess_pts.get(fvg["session"], 5)
    detail["session"] = pts
    score += pts
    # 4) Middle-candle body quality (10)
    mc = fvg["middle_candle"]
    body = abs(mc["close"] - mc["open"]); rng = mc["high"] - mc["low"]
    br = body / rng if rng > 0 else 0
    pts = 10 if br >= 0.7 else 5 if br >= 0.5 else 0
    detail["candle"] = pts
    detail["body_ratio"] = round(br, 2)
    score += pts
    # 5) DXY placeholder (20) — neutral 10 when unavailable
    detail["dxy"] = 10
    score += detail["dxy"]
    label = "high" if score >= 70 else "medium" if score >= 45 else "low"
    return score, label, detail

def is_reversal(candles, direction, lookback=2):
    for c in candles[-lookback:]:
        body = c["close"] - c["open"]; rng = c["high"] - c["low"]
        if rng <= 0: continue
        if abs(body) / rng < 0.4: continue
        if direction == "bullish" and body > 0: return True
        if direction == "bearish" and body < 0: return True
    return False

TF_SEC = {"5m": 300, "15m": 900, "1h": 3600}
MIN_SIGNAL_SCORE = 60
FVG_MAX_AGE = 12 * 3600
SESSION_AR = {"asia": "آسيا (طوكيو/سيدني)", "london": "لندن", "newyork": "نيويورك", "overlap": "تقاطع لندن ونيويورك"}

# ---- H1: from API if possible, otherwise built from closed M15 candles ----
_h1_block_until = 0.0

def resample(candles, sec):
    per = max(1, int(sec // 900))
    buckets = {}
    for c in candles:
        buckets.setdefault(int(c["time"] // sec) * sec, []).append(c)
    out = []
    for t in sorted(buckets):
        g = buckets[t]
        if len(g) < per: continue                   # incomplete hour -> not closed yet
        out.append({"time": float(t), "open": g[0]["open"], "high": max(x["high"] for x in g),
                    "low": min(x["low"] for x in g), "close": g[-1]["close"]})
    return out

def fetch_h1(m15):
    global _h1_block_until
    if now_ts() >= _h1_block_until:
        try:
            return fetch_candles("1h")
        except Exception as e:
            _h1_block_until = now_ts() + 600       # don't retry the API every 8 seconds
            log("1h API failed, building H1 from M15:", e)
    return resample(m15, 3600)

# ---- MACD (real EMA calculation) ----
def ema_series(values, period):
    if len(values) < period: return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def macd_calc(candles, fast=12, slow=26, sig=9):
    closes = [c["close"] for c in candles]
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    if not es: return None
    off = slow - fast
    line = [ef[i + off] - es[i] for i in range(len(es))]
    sg = ema_series(line, sig)
    if len(sg) < 2: return None
    hist = [line[i + sig - 1] - sg[i] for i in range(len(sg))]
    return {"macd": line[-1], "signal": sg[-1], "hist": hist[-1], "prev_hist": hist[-2],
            "cross": (hist[-2] <= 0 < hist[-1]) or (hist[-2] >= 0 > hist[-1])}

def _macd_turn(m, direction):
    """Gate for M5: momentum already in direction, or turning toward it."""
    if not m: return False
    if direction == "BUY": return m["hist"] > 0 or m["hist"] > m["prev_hist"]
    return m["hist"] < 0 or m["hist"] < m["prev_hist"]

def _macd_side(m, direction):
    return bool(m) and ((m["hist"] > 0) if direction == "BUY" else (m["hist"] < 0))

# ---- swing points, support / resistance, liquidity sweeps ----
def swing_points(candles, left=3, right=3):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        w = candles[i - left:i + right + 1]
        if candles[i]["high"] >= max(x["high"] for x in w): highs.append((i, candles[i]["high"]))
        if candles[i]["low"] <= min(x["low"] for x in w): lows.append((i, candles[i]["low"]))
    return highs, lows

def build_levels(h1, m15, price, atr15):
    tol = max(atr15 * 0.35, 0.3)
    pts = []
    for tf, cs, w, wt in (("1h", h1, 3, 2), ("15m", m15, 4, 1)):
        hs, ls = swing_points(cs, w, w)
        for _, p in hs + ls: pts.append((p, tf, wt))
    pts.sort()
    clusters = []
    for p, tf, wt in pts:
        if clusters and p - clusters[-1]["avg"] <= tol:
            c = clusters[-1]
            c["ps"].append(p); c["w"] += wt
            c["avg"] = sum(c["ps"]) / len(c["ps"]); c["h1"] = c["h1"] or tf == "1h"
        else:
            clusters.append({"ps": [p], "w": wt, "avg": p, "h1": tf == "1h"})
    levels = [{"price": round(c["avg"], 2), "strength": c["w"], "tf": "1h" if c["h1"] else "15m",
               "kind": "resistance" if c["avg"] > price else "support"}
              for c in clusters if c["w"] >= 2]
    levels.sort(key=lambda l: abs(l["price"] - price))
    sup = [l for l in levels if l["kind"] == "support"][:3]
    res = [l for l in levels if l["kind"] == "resistance"][:3]
    return sorted(sup + res, key=lambda l: abs(l["price"] - price))

def detect_sweeps(candles, tf, lookback=60, recent=6):
    """Wick beyond a recent swing high/low, close back inside = liquidity sweep."""
    if len(candles) < lookback + recent: return []
    ref, rec = candles[-(lookback + recent):-recent], candles[-recent:]
    hs, ls = swing_points(ref, 3, 3)
    out = []
    for _, lvl in hs[-5:]:
        hit = [c for c in rec if c["high"] > lvl and c["close"] < lvl]
        if hit and not any(c["close"] > lvl for c in rec):
            out.append({"side": "SELL", "tf": tf, "level": round(lvl, 2), "time": hit[-1]["time"]})
    for _, lvl in ls[-5:]:
        hit = [c for c in rec if c["low"] < lvl and c["close"] > lvl]
        if hit and not any(c["close"] < lvl for c in rec):
            out.append({"side": "BUY", "tf": tf, "level": round(lvl, 2), "time": hit[-1]["time"]})
    out.sort(key=lambda x: x["time"], reverse=True)
    return out

# ---- retest confirmation ----
def _retest(direction, lo, hi, m5, start, tol):
    """Zone must be touched in the last 3 closed M5 candles AND the last candle must close
    back out of the zone in the trade direction with a real body."""
    last = [c for c in m5[-3:] if c["time"] >= start]
    c = m5[-1]
    if not last or c["time"] < start: return None
    rng, body = c["high"] - c["low"], c["close"] - c["open"]
    br = abs(body) / rng if rng > 0 else 0
    if direction == "BUY":
        touched = any(x["low"] <= hi + tol for x in last)
        broke = any(x["close"] < lo for x in last)
        confirm = body > 0 and br >= 0.4 and c["close"] >= hi
        extreme = min(x["low"] for x in last)
    else:
        touched = any(x["high"] >= lo - tol for x in last)
        broke = any(x["close"] > hi for x in last)
        confirm = body < 0 and br >= 0.4 and c["close"] <= lo
        extreme = max(x["high"] for x in last)
    if broke or not touched or not confirm: return None
    return {"br": br, "extreme": extreme}

# ---- scoring /100 with reasons ----
def score_setup(direction, f, kind, br, entry, risk, rr, ctx):
    buy = direction == "BUY"
    want = "BULLISH" if buy else "BEARISH"
    word = "صاعد" if buy else "هابط"
    bd, why, warn = {}, [], []
    # 1) trend H1 + M15 (20)
    t1, t15 = ctx["trends"]["1h"], ctx["trends"]["15m"]
    p1 = 12 if t1 == want else 5 if t1 == "NEUTRAL" else 0
    p15 = 8 if t15 == want else 3 if t15 == "NEUTRAL" else 0
    bd["trend"] = p1 + p15
    if t1 == want: why.append(f"اتجاه H1 {word} متوافق (+{p1})")
    elif t1 == "NEUTRAL": why.append(f"H1 محايد (+{p1})")
    if t15 == want: why.append(f"اتجاه M15 {word} متوافق (+{p15})")
    else: warn.append("اتجاه M15 لا يدعم الصفقة")
    # 2) MACD (15)
    m15, m5m = ctx["macd"].get("15m"), ctx["macd"].get("5m")
    pm = 0
    if _macd_side(m15, direction):
        pm += 6; why.append("MACD على M15 يدعم الاتجاه (+6)")
    else:
        warn.append("MACD على M15 لا يدعم الاتجاه")
    if _macd_side(m5m, direction):
        pm += 5; why.append("MACD على M5 في اتجاه الصفقة (+5)")
    if m5m and ((m5m["hist"] > m5m["prev_hist"]) if buy else (m5m["hist"] < m5m["prev_hist"])):
        pm += 4; why.append("زخم MACD على M5 يتحسن لصالح الصفقة (+4)")
    bd["macd"] = pm
    # 3) zone size (10)
    ratio = f["size"] / f["atr_at_form"] if f.get("atr_at_form", 0) > 0 else 0
    pz = 10 if ratio >= 1 else 7 if ratio >= 0.5 else 4 if ratio >= 0.25 else 1
    bd["zone"] = pz
    why.append(f"حجم {kind} = {round(ratio, 2)}× ATR (+{pz})")
    # 4) support / resistance confluence (15)
    lo, hi, pad = f["bottom"], f["top"], ctx["atr15"] * 0.5
    kind_sr = "support" if buy else "resistance"
    near = [l for l in ctx["levels"] if l["kind"] == kind_sr and lo - pad <= l["price"] <= hi + pad]
    psr = 0
    if near:
        b = max(near, key=lambda l: l["strength"])
        psr = 15 if b["strength"] >= 3 else 10
        why.append(f"المنطقة عند {'دعم' if buy else 'مقاومة'} {b['tf'].upper()} {b['price']} (+{psr})")
    bd["sr"] = psr
    # 5) liquidity sweep (15)
    sw = [s for s in ctx["sweeps"] if s["side"] == direction]
    psw = 0
    if sw:
        s0 = max(sw, key=lambda s: (s["tf"] == "15m", s["time"]))
        psw = 15 if s0["tf"] == "15m" else 12
        why.append(f"سحب سيولة {'قيعان' if buy else 'قمم'} على {s0['tf'].upper()} عند {s0['level']} (+{psw})")
    bd["sweep"] = psw
    # 6) session (10)
    ps = {"overlap": 10, "london": 9, "newyork": 8, "asia": 3}.get(ctx["session"], 3)
    bd["session"] = ps
    why.append(f"جلسة {SESSION_AR.get(ctx['session'], ctx['session'])} (+{ps})")
    # 7) retest candle quality (10)
    pr = 10 if br >= 0.7 else 7 if br >= 0.5 else 4
    bd["retest"] = pr
    why.append(f"شمعة التأكيد جسمها {int(br * 100)}% من المدى (+{pr})")
    # 8) room to target before opposing S/R (5)
    opp = [l for l in ctx["levels"] if (l["price"] > entry if buy else l["price"] < entry)]
    proom = 5
    if opp:
        n = min(opp, key=lambda l: abs(l["price"] - entry))
        dist = abs(n["price"] - entry)
        name = "مقاومة" if buy else "دعم"
        if dist >= risk * rr: why.append("لا عوائق S/R قبل الهدف (+5)")
        elif dist >= risk: proom = 2; warn.append(f"{name} عند {n['price']} قبل الهدف الكامل")
        else: proom = 0; warn.append(f"{name} قريب عند {n['price']} (أقل من 1R)")
    else:
        why.append("طريق مفتوح بلا S/R قبل الهدف (+5)")
    bd["room"] = proom
    total = sum(bd.values())
    label = "high" if total >= 75 else "medium" if total >= 60 else "low"
    return total, label, bd, why, warn

# ---- the signal picker ----
def pick_signal(fvgs, m5, ctx, rr=2.0, buffer_mult=0.15):
    """Returns (signal | None, blocked[]). A signal needs: confirmed M5 retest of an FVG/IFVG,
    H1 not against it, MACD M5 agreeing/turning, score >= MIN_SIGNAL_SCORE, risk within cap."""
    if not m5: return None, []
    now_t, price = m5[-1]["time"], m5[-1]["close"]
    atr5 = ctx["atr5"]
    tol, buf = atr5 * 0.1, atr5 * buffer_mult
    good, blocked = [], []
    for f in fvgs:
        if f["status"] in ("active", "partial"):
            kind, ref = "FVG", f["formed_at"]
            direction = "BUY" if f["type"] == "bullish" else "SELL"
        elif f["status"] == "ifvg" and f.get("ifvg_at"):
            kind, ref = "IFVG", f["ifvg_at"]
            direction = "SELL" if f["type"] == "bullish" else "BUY"
        else:
            continue
        if now_t - ref > FVG_MAX_AGE: continue
        start = ref + TF_SEC.get(f["tf"], 300)
        lo, hi = f["bottom"], f["top"]
        if kind == "IFVG":     # an inverted zone dies if price closes back through it
            after = [c for c in m5 if c["time"] >= start]
            if direction == "SELL" and any(c["close"] > hi for c in after): continue
            if direction == "BUY" and any(c["close"] < lo for c in after): continue
        rt = _retest(direction, lo, hi, m5, start, tol)
        if not rt: continue                         # no confirmed retest -> no signal
        entry = price
        if direction == "BUY":
            stop = min(lo, rt["extreme"]) - buf
            if stop >= entry: continue
            risk = entry - stop; target = entry + risk * rr
        else:
            stop = max(hi, rt["extreme"]) + buf
            if stop <= entry: continue
            risk = stop - entry; target = entry - risk * rr
        sc, label, bd, why, warn = score_setup(direction, f, kind, rt["br"], entry, risk, rr, ctx)
        why.insert(0, "إعادة اختبار مؤكدة على M5: شمعة " + ("صاعدة" if direction == "BUY" else "هابطة")
                   + " لمست المنطقة وأغلقت خارجها")
        opp_trend = "BEARISH" if direction == "BUY" else "BULLISH"
        reason = None
        if ctx["trends"]["1h"] == opp_trend: reason = "ضد اتجاه H1"
        elif not _macd_turn(ctx["macd"].get("5m"), direction): reason = "MACD على M5 لا يؤكد الاتجاه"
        elif sc < MIN_SIGNAL_SCORE: reason = f"الدرجة {sc} أقل من الحد الأدنى {MIN_SIGNAL_SCORE}"
        if reason:
            blocked.append({"side": direction, "kind": kind, "tf": f["tf"], "score": sc, "reason": reason})
            continue
        # Signal-only output levels: first target at 1R, second target at configured R:R.
        target1 = entry + risk if direction == "BUY" else entry - risk
        target2 = entry + (risk * rr) if direction == "BUY" else entry - (risk * rr)
        good.append({
            "side": direction, "kind": kind, "tf": f["tf"],
            "entry": round(entry, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "zone_low": round(lo, 2), "zone_high": round(hi, 2),
            "fvg_id": f["id"], "score": sc, "label": label, "breakdown": bd,
            "reasons": why, "warnings": warn, "time": ref,
        })
    blocked.sort(key=lambda b: b["score"], reverse=True)
    if not good: return None, blocked[:3]
    good.sort(key=lambda x: x["score"], reverse=True)
    return good[0], blocked[:3]

# ───────────────────────── analysis endpoint ────────────────────
def analyze(rr=2.0, buffer_mult=0.15):
    rr = 2.0
    buffer_mult = 0.15

    m5  = fetch_candles("5m")
    m15 = fetch_candles("15m")
    h1  = fetch_h1(m15)
    atr5, atr15 = atr(m5, 14), atr(m15, 14)
    trends = {"1h": trend_from(h1), "15m": trend_from(m15), "5m": trend_from(m5)}
    trend_m15, trend_m5 = trends["15m"], trends["5m"]
    macds = {"1h": macd_calc(h1), "15m": macd_calc(m15), "5m": macd_calc(m5)}
    price = m5[-1]["close"]
    levels = build_levels(h1, m15, price, atr15)
    sweeps = detect_sweeps(m5, "5m", 60, 12) + detect_sweeps(m15, "15m", 60, 4)
    session = session_of(m5[-1]["time"])
    ctx = {"trends": trends, "macd": macds, "levels": levels, "sweeps": sweeps,
           "atr5": atr5, "atr15": atr15, "session": session}
    now_t = m5[-1]["time"]

    with _state_lock:
        # 1) detect (scan a window so nothing is missed between polls / after restart)
        known = {f["id"] for f in _fvgs}
        for new_f in (detect_new_fvgs(m5, "5m", atr5, SCAN_BACK) +
                      detect_new_fvgs(m15, "15m", atr15, SCAN_BACK)):
            if new_f["id"] not in known:
                known.add(new_f["id"])
                _fvgs.append(new_f)
        # 2) refresh status + score of everything (new ones included)
        for fvg in _fvgs:
            src = m5 if fvg["tf"] == "5m" else m15
            fvg["status"] = update_fvg_status(fvg, src)
            if fvg["status"] in ("active", "partial"):
                s, l, d = compute_gold_score(fvg, trend_m15, trend_m5)
                fvg["score"], fvg["label"], fvg["score_detail"] = s, l, d
        # 3) prune (newest first)
        _fvgs.sort(key=lambda x: x["formed_at"], reverse=True)
        del _fvgs[MAX_FVGS:]

        signal, blocked = pick_signal(_fvgs, m5, ctx, rr, buffer_mult)
        shown = []
        for f in _fvgs:
            if f["status"] in ("active", "partial"):
                d = {k: v for k, v in f.items() if k != "middle_candle"}
                d["kind"], d["bias"] = "FVG", f["type"]
                shown.append(d)
            elif f["status"] == "ifvg" and f.get("ifvg_at") and now_t - f["ifvg_at"] <= FVG_MAX_AGE:
                d = {k: v for k, v in f.items() if k != "middle_candle"}
                d["kind"], d["bias"] = "IFVG", ("bearish" if f["type"] == "bullish" else "bullish")
                shown.append(d)
        persist_state()

    macd_out = {k: ({kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()} if v else None)
                for k, v in macds.items()}
    if signal: msg = ""
    elif blocked: msg = "إعداد محجوب: " + blocked[0]["reason"]
    else: msg = "لا توجد فرصة مؤكدة. في انتظار إعادة اختبار FVG/IFVG بشمعة تأكيد."
    return {
        "symbol": "XAUUSD",
        "trend": trend_m15, "trend_m5": trend_m5, "trends": trends,
        "macd": macd_out, "levels": levels, "sweeps": sweeps[:3],
        "last_price": round(price, 2),
        "last_time": datetime.fromtimestamp(m5[-1]["time"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "atr": round(atr5, 2),
        "session": session,
        "buffer_multiplier": buffer_mult,
        "rr": rr,
        "signal": signal,
        "blocked": blocked,
        "active_fvgs": shown,
        "candles": {"5m": m5[-260:], "15m": m15[-200:], "1h": h1[-150:]},
        "message": msg,
    }

# ───────────────────────── HTML ─────────────────────────────────
HTML = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="theme-color" content="#04060d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="__APP_NAME__">
<title>__APP_NAME__ · XAU/USD</title>
<style>
:root{color-scheme:dark;--bg:#04060d;--text:#eef2fb;--muted:#8a96b2;--gold:#f4ca69;--gold-deep:#d4a53f;--green:#3ddc97;--red:#ff5566;--border:rgba(255,255,255,.09);--border-hi:rgba(255,255,255,.2);--surface:rgba(255,255,255,.05);--glass:linear-gradient(145deg,rgba(255,255,255,.13),rgba(255,255,255,.035));--blur:blur(26px) saturate(185%);--shine:inset 0 1px 0 rgba(255,255,255,.24),inset 0 -1px 0 rgba(255,255,255,.04),0 10px 30px rgba(0,0,0,.38)}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}html{background:var(--bg)}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;color:var(--text);min-height:100vh;padding-bottom:130px;background:radial-gradient(circle at 12% -5%,rgba(244,202,105,.22),transparent 42%),radial-gradient(circle at 105% 30%,rgba(70,110,255,.16),transparent 45%),radial-gradient(circle at 20% 105%,rgba(61,220,151,.12),transparent 45%),#04060d;background-attachment:fixed;-webkit-font-smoothing:antialiased}
.glass,.hero-card,.mini-card,.signal-card,.card,.journal-item,.fvg-item,.disclaimer{background:var(--glass);border:1px solid var(--border);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);box-shadow:var(--shine)}
.appbar{position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;align-items:center;padding:calc(env(safe-area-inset-top,0px) + 30px) 18px 12px;background:linear-gradient(180deg,rgba(4,6,13,.82),rgba(4,6,13,.45));backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%);border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:40px;height:40px;border-radius:13px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;color:#2a1d00;background:linear-gradient(145deg,#ffe49a,#d4a53f);box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 6px 18px rgba(244,202,105,.35)}
.brand-title{font-size:19px;font-weight:800;letter-spacing:.3px}
.brand-sub{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:1.2px;font-weight:600}
.baghdad-clock{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 15px;margin-bottom:12px;border-radius:18px;background:linear-gradient(120deg,rgba(244,202,105,.12),rgba(18,30,48,.5));border:1px solid rgba(244,202,105,.2)}
.baghdad-clock span{font-size:12px;color:var(--muted);font-weight:700}
.baghdad-clock strong{font-size:18px;font-variant-numeric:tabular-nums;letter-spacing:1px;color:var(--gold)}
.baghdad-clock small{font-size:10px;color:var(--muted)}
.market-liquidity-title{font-size:13px;font-weight:800;color:var(--gold);margin:16px 0 8px;padding-top:12px;border-top:1px solid var(--border)}
.status-pill{font-size:11px;padding:8px 13px;border-radius:20px;background:rgba(61,220,151,.12);color:var(--green);border:1px solid rgba(61,220,151,.3);font-weight:700;display:flex;align-items:center;gap:7px;box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}
.status-pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;animation:pulse 2s infinite}
.status-pill.stale{background:rgba(255,85,102,.12);color:var(--red);border-color:rgba(255,85,102,.3)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
main{max-width:720px;margin:0 auto;padding:16px 16px 0}
.page{display:none}.page.active{display:block;animation:fade .3s ease}
@keyframes fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.hero-card{border-radius:30px;padding:24px;margin-bottom:14px;position:relative;overflow:hidden}
.hero-card::before,.signal-card::before{content:"";position:absolute;top:0;left:12%;right:12%;height:1px;background:linear-gradient(90deg,transparent,rgba(244,202,105,.7),transparent)}
.label{font-size:10px;color:var(--muted);letter-spacing:1.3px;text-transform:uppercase;font-weight:700}
.hero-price{font-size:46px;font-weight:800;letter-spacing:-2px;margin:8px 0 4px;font-variant-numeric:tabular-nums;line-height:1.05;transition:color .15s}
.hero-price.gold{background:linear-gradient(135deg,#fff0b8,#f4ca69 45%,#d4a53f);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-price.tick-up{-webkit-text-fill-color:#3ddc97;background:none;color:#3ddc97}
.hero-price.tick-down{-webkit-text-fill-color:#ff5566;background:none;color:#ff5566}
.hero-meta{font-size:12px;color:var(--muted);display:flex;justify-content:space-between;margin-top:12px}
.chip{display:inline-block;margin-top:14px;font-size:10px;padding:5px 10px;border-radius:10px;background:rgba(244,202,105,.1);color:var(--gold);border:1px solid rgba(244,202,105,.25);font-weight:700}
.row-2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.mini-card{border-radius:22px;padding:16px}
.mini-card .value{font-size:19px;font-weight:700;margin-top:8px;font-variant-numeric:tabular-nums}
.trend-badge{display:inline-flex;align-items:center;gap:6px;padding:7px 11px;border-radius:12px;font-size:12px;font-weight:700;margin-top:8px;line-height:1.5}
.trend-badge.bullish{background:rgba(61,220,151,.13);color:var(--green);border:1px solid rgba(61,220,151,.32)}
.trend-badge.bearish{background:rgba(255,85,102,.13);color:var(--red);border:1px solid rgba(255,85,102,.32)}
.trend-badge.neutral{background:rgba(138,150,178,.15);color:var(--muted);border:1px solid rgba(138,150,178,.3)}
.signal-card{border-radius:30px;padding:22px;margin-bottom:14px;position:relative;overflow:hidden;transition:border-color .3s,box-shadow .3s}
.signal-card.buy{border-color:rgba(61,220,151,.35);box-shadow:var(--shine),0 0 40px rgba(61,220,151,.1)}
.signal-card.sell{border-color:rgba(255,85,102,.35);box-shadow:var(--shine),0 0 40px rgba(255,85,102,.1)}
.signal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
.side-badge{font-size:11px;font-weight:800;padding:8px 15px;border-radius:14px;letter-spacing:1.5px;display:none}
.side-badge.show{display:inline-block;animation:pop .3s ease}
@keyframes pop{from{transform:scale(.8);opacity:0}to{transform:scale(1);opacity:1}}
.side-badge.buy{background:rgba(61,220,151,.2);color:var(--green);border:1px solid rgba(61,220,151,.45)}
.side-badge.sell{background:rgba(255,85,102,.2);color:var(--red);border:1px solid rgba(255,85,102,.45)}
.signal-levels{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.level{text-align:center;padding:14px 6px;background:rgba(4,6,13,.45);border-radius:18px;border:1px solid var(--border);box-shadow:inset 0 1px 0 rgba(255,255,255,.06)}
.level .level-label{font-size:9px;color:var(--muted);letter-spacing:.8px;margin-bottom:6px;font-weight:700}
.level .level-value{font-size:16px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.level.entry .level-value{color:var(--gold)}.level.stop .level-value{color:var(--red)}.level.target .level-value{color:var(--green)}
.signal-meta{margin-top:16px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:11px;color:var(--muted)}
.signal-meta strong{color:var(--text);font-weight:700}
.score-row{margin-top:12px;padding-top:12px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;font-size:12px}
.score-badge{padding:6px 12px;border-radius:12px;font-weight:800;font-size:12px}
.score-badge.high{background:rgba(61,220,151,.18);color:var(--green)}
.score-badge.medium{background:rgba(244,202,105,.18);color:var(--gold)}
.score-badge.low{background:rgba(138,150,178,.15);color:var(--muted)}
.empty-state{text-align:center;padding:20px 10px;color:var(--muted);font-size:13px;line-height:1.7}
.empty-state .big-icon{font-size:38px;opacity:.35;margin-bottom:8px;display:block}
.empty-state strong{display:block;color:var(--text);font-size:14px;margin-bottom:4px}
.btn{width:100%;padding:15px;border-radius:20px;border:1px solid var(--border-hi);background:var(--glass);color:var(--text);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;position:relative;overflow:hidden;backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);box-shadow:var(--shine);transition:transform .15s,filter .2s}
.btn::after{content:"";position:absolute;inset:0;border-radius:inherit;background:linear-gradient(180deg,rgba(255,255,255,.2),transparent 52%);pointer-events:none}
.btn:active{transform:scale(.96);filter:brightness(1.15)}
.btn-primary{background:linear-gradient(145deg,rgba(255,221,130,.6),rgba(212,165,63,.3));border-color:rgba(244,202,105,.6);color:#fff7e0;box-shadow:var(--shine),0 8px 28px rgba(244,202,105,.25)}
.btn-ghost{margin-top:10px}
.actions{display:grid;grid-template-columns:1.2fr 1fr;gap:10px;margin-bottom:6px}.actions .btn{margin-top:0}
nav{position:fixed;bottom:max(14px,env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);width:min(420px,calc(100% - 28px));padding:7px;border-radius:32px;background:linear-gradient(160deg,rgba(255,255,255,.15),rgba(255,255,255,.04));border:1px solid rgba(255,255,255,.2);box-shadow:0 14px 44px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.28);backdrop-filter:blur(40px) saturate(200%);-webkit-backdrop-filter:blur(40px) saturate(200%);display:flex;gap:4px;z-index:100}
nav button{flex:1;padding:9px 4px 8px;border:none;background:transparent;color:rgba(238,242,251,.5);border-radius:25px;font-size:10px;font-weight:700;font-family:inherit;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;transition:all .3s}
nav button svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
nav button.active{background:linear-gradient(160deg,rgba(244,202,105,.28),rgba(244,202,105,.07));color:var(--gold);box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 4px 16px rgba(244,202,105,.18)}
canvas{width:100%;height:320px;display:block;border-radius:22px;background:rgba(4,6,13,.55);border:1px solid var(--border);touch-action:none;-webkit-user-select:none;user-select:none}
.chart-wrap{position:relative}
.chart-hint{position:absolute;bottom:4px;left:10px;font-size:10px;color:var(--muted);background:rgba(4,6,13,.7);padding:5px 9px;border-radius:10px;border:1px solid var(--border);pointer-events:none}
.chart-live-tag{position:absolute;top:10px;left:92px;font-size:10px;color:var(--green);background:rgba(61,220,151,.12);padding:5px 9px;border-radius:10px;border:1px solid rgba(61,220,151,.3);pointer-events:none;font-weight:700;display:flex;align-items:center;gap:5px}
.chart-live-tag::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor;box-shadow:0 0 6px currentColor;animation:pulse 1.5s infinite}
.chart-reset,.chart-cross{position:absolute;top:8px;width:34px;height:34px;border-radius:12px;background:var(--glass);border:1px solid var(--border-hi);color:var(--text);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:inset 0 1px 0 rgba(255,255,255,.25)}
.chart-reset{left:8px}.chart-cross{left:50px}
.chart-cross.active{background:rgba(244,202,105,.3);border-color:var(--gold);color:var(--gold)}
.card{border-radius:26px;padding:18px;margin-bottom:14px}
.card h3{font-size:14px;margin-bottom:14px;font-weight:700}
.row{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid var(--border);font-size:13px}
.row:last-child{border-bottom:none}
.row .row-label{color:var(--muted)}.row .row-value{font-weight:700;font-variant-numeric:tabular-nums}
label{display:block;font-size:12px;color:var(--muted);margin:16px 0 6px;font-weight:700}
input,select{width:100%;padding:14px;background:rgba(4,6,13,.55);border:1px solid var(--border-hi);border-radius:16px;color:var(--text);font-size:15px;font-family:inherit;font-weight:600;transition:border .2s,box-shadow .2s;box-shadow:inset 0 1px 3px rgba(0,0,0,.4)}
input:focus,select:focus{outline:none;border-color:var(--gold);box-shadow:0 0 0 3px rgba(244,202,105,.15)}
.check-label{display:flex;align-items:center;gap:10px;cursor:pointer;margin-top:16px}
.check-label input[type="checkbox"]{width:auto;padding:0;margin:0;transform:scale(1.3)}
.journal-item{border-radius:20px;padding:14px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.j-side{font-size:11px;font-weight:800;padding:6px 10px;border-radius:10px;white-space:nowrap}
.j-side.buy{background:rgba(61,220,151,.15);color:var(--green)}.j-side.sell{background:rgba(255,85,102,.15);color:var(--red)}
.j-body{flex:1;font-size:12px;color:var(--muted);line-height:1.6}
.j-body strong{color:var(--text);font-variant-numeric:tabular-nums;font-weight:700}
.j-time{font-size:10px;color:var(--muted);margin-top:4px;opacity:.8}
.j-del{background:transparent;border:none;color:var(--muted);font-size:16px;padding:8px;cursor:pointer}
.disclaimer{border-radius:20px;padding:14px;font-size:11px;color:rgba(244,202,105,.92);line-height:1.8;margin-bottom:14px;border-color:rgba(244,202,105,.25)}
.disclaimer strong{color:var(--gold);font-weight:800}
.hint{font-size:11px;color:var(--muted);line-height:1.7;margin-top:10px}
.updated{text-align:center;font-size:11px;color:var(--muted);padding:12px 0}
.section-title{font-size:12px;font-weight:800;color:var(--gold);margin-top:22px;margin-bottom:8px;letter-spacing:.5px;padding-top:14px;border-top:1px solid var(--border)}
.fvg-item{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-radius:16px;margin-bottom:8px;border-right:3px solid;box-shadow:none}
.fvg-item.bull{border-right-color:var(--green)}.fvg-item.bear{border-right-color:var(--red)}
.fvg-type{font-size:16px;font-weight:800;margin-left:8px}
.fvg-info{flex:1;font-size:12px}
.fvg-meta{font-size:10px;color:var(--muted);margin-top:3px}
.fvg-score{font-size:10px;padding:4px 8px;border-radius:8px;font-weight:800}
.fvg-score.high{background:rgba(61,220,151,.18);color:var(--green)}
.fvg-score.medium{background:rgba(244,202,105,.18);color:var(--gold)}
.fvg-score.low{background:rgba(138,150,178,.15);color:var(--muted)}
.liquidity-item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px;border-radius:15px;margin-bottom:8px;background:rgba(255,255,255,.045);border:1px solid var(--border);font-size:12px}
.liquidity-item.buy{border-right:3px solid var(--green)}
.liquidity-item.sell{border-right:3px solid var(--red)}
.liquidity-item .liq-side{font-weight:800}
.liquidity-item .liq-meta{color:var(--muted);font-size:10px;margin-top:4px}

.tf-row{display:flex;gap:6px;margin-bottom:12px;padding:4px;border-radius:18px;background:rgba(4,6,13,.45);border:1px solid var(--border)}
.tf-row button{flex:1;padding:9px;border-radius:14px;border:none;background:transparent;color:var(--muted);font-weight:700;font-size:12px;font-family:inherit;cursor:pointer;transition:all .2s}
.tf-row button.active{background:var(--glass);color:var(--gold);box-shadow:inset 0 1px 0 rgba(255,255,255,.28),0 4px 12px rgba(0,0,0,.3)}
.reasons{margin-top:14px;padding-top:12px;border-top:1px solid var(--border);font-size:12px;line-height:1.8}
.reasons ul{list-style:none;padding:0}.reasons li{padding:3px 0}
.reasons li::before{content:"✓ ";color:var(--green);font-weight:800}
.reasons .warn{color:var(--gold);font-size:11px;margin-top:4px}
.blocked{margin-top:10px;padding:10px 12px;border-radius:14px;background:rgba(255,85,102,.09);border:1px solid rgba(255,85,102,.28);color:var(--red);font-size:12px;line-height:1.6;text-align:right}
@media(min-width:600px){.row-2{grid-template-columns:1fr 1fr}}
</style>
</style>
</head>
<body>
<header class="appbar">
  <div class="brand">
    <div class="logo">Au</div>
    <div><div class="brand-title">__APP_NAME__</div><div class="brand-sub">XAU/USD · تحليل ذكي</div></div>
  </div>
  <div class="status-pill" id="status">تحليل</div>
</header>
<main>
<section id="home" class="page active">
  <div class="baghdad-clock card">
    <span>توقيت بغداد</span>
    <strong id="baghdadTime">--:--:--</strong>
    <small id="baghdadDate">—</small>
  </div>
  <div class="hero-card">
    <div class="label">الذهب · XAU/USD</div>
    <div class="hero-price gold" id="price">—</div>
    <div class="hero-meta"><span id="priceTime">—</span><span class="bid-ask" id="bidAsk">—</span></div>
  </div>
  <div class="signal-card" id="signalCard">
    <div class="signal-head">
      <div class="label">الإشارة الحالية</div>
      <div class="side-badge" id="sideBadge">—</div>
    </div>
    <div id="signalBody"><div class="empty-state"><span class="big-icon">◌</span>جارِ تحليل السوق...</div></div>
  </div>
  <div class="row-2">
    <div class="mini-card"><div class="label">الاتجاه H1 · M15 · M5</div><div class="trend-badge neutral" id="trendBadge">—</div></div>
    <div class="mini-card"><div class="label">السبريد</div><div class="value" id="spread">—</div></div>
  </div>
  <div class="updated" id="lastUpdate">—</div>
</section>
<section id="chart" class="page">
  <div class="card">
    <h3>شارت <span id="chartTfLabel">M5</span> · تحديث حي</h3>
    <div class="tf-row" id="tfRow"><button data-tf="5m" class="active">M5</button><button data-tf="15m">M15</button><button data-tf="1h">H1</button></div>
    <div class="chart-wrap">
      <canvas id="chartCanvas" width="800" height="320"></canvas>
      <div class="chart-hint">اسحب · قرّب · ⟲ لإعادة الضبط</div>
      <div class="chart-live-tag" id="liveTag">حي</div>
      <button class="chart-cross" id="crossBtn" title="تحليل">✛</button>
      <button class="chart-reset" id="chartReset" title="إعادة ضبط">⟲</button>
    </div>
  </div>
  <div class="card">
    <h3>معلومات السوق</h3>
    <div class="row"><span class="row-label">ATR (14)</span><strong class="row-value" id="atrVal">—</strong></div>
    <div class="row"><span class="row-label">الجلسة</span><strong class="row-value" id="sessionVal">—</strong></div>
    <div class="row"><span class="row-label">الاتجاه M5</span><strong class="row-value" id="trendM5">—</strong></div>
    <div class="row"><span class="row-label">الشموع</span><strong class="row-value" id="barCount">—</strong></div>
    <div class="row"><span class="row-label">MACD H1</span><strong class="row-value" id="macd1h">—</strong></div>
    <div class="row"><span class="row-label">MACD M15</span><strong class="row-value" id="macd15m">—</strong></div>
    <div class="row"><span class="row-label">MACD M5</span><strong class="row-value" id="macd5m">—</strong></div>
    <div class="row"><span class="row-label">أقرب دعم</span><strong class="row-value" id="srSup">—</strong></div>
    <div class="row"><span class="row-label">أقرب مقاومة</span><strong class="row-value" id="srRes">—</strong></div>
    <div class="market-liquidity-title">السيولة</div>
    <div id="liquidityList"><div class="empty-state">جارٍ فحص السيولة...</div></div>
  </div>
  <div class="card">
    <h3>المناطق النشطة (<span id="fvgCount">0</span>)</h3>
    <div id="fvgList"><div class="empty-state">جارٍ الفحص...</div></div>
  </div>

</section>
<section id="journal" class="page">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <h3 style="margin:0">سجل الإشارات</h3>
      <button class="btn btn-ghost" id="clearBtn" style="width:auto;padding:8px 14px;font-size:12px;margin:0">مسح</button>
    </div>
    <div id="journalList"><div class="empty-state"><span class="big-icon">▤</span>لا توجد إشارات محفوظة</div></div>
  </div>
</section>
<section id="settings" class="page">
  <div class="card">
    <h3>إعدادات الإشارة</h3>
    <p class="hint">هذا التطبيق للتحليل وإظهار الإشارات فقط، ولا ينفّذ أي صفقات.</p>
    <div class="section-title">ربط تلغرام</div>
    <label>Bot Token</label>
    <input id="tgToken" type="text" placeholder="123456789:ABCdef..." autocomplete="off">
    <label>Chat ID</label>
    <input id="tgChatId" type="text" placeholder="-1001234567890" autocomplete="off">
    <label class="check-label"><input id="tgAuto" type="checkbox">إرسال تلقائي عند ظهور إشارة جديدة</label>
    <button class="btn btn-ghost" id="tgTestBtn" style="margin-top:14px">اختبار الإرسال</button>
    <p class="hint">Chat ID من <strong>@userinfobot</strong> · Token من <strong>@BotFather</strong>.</p>
    <button class="btn btn-primary" id="saveSettingsBtn" style="margin-top:20px">حفظ الإعدادات</button>
  </div>
  <div class="disclaimer">
    <strong>تنبيه:</strong> تطبيق للتحليل فقط. لا يفتح صفقات. راجع دائماً السعر والسبريد في MT5 قبل أي قرار. Gold Score تقديري وليس ضماناً.
  </div>
  <div class="card">
    <h3>مصدر البيانات</h3>
    <div class="row"><span class="row-label">المصدر</span><strong class="row-value">biquote.io</strong></div>
    <div class="row"><span class="row-label">الرمز</span><strong class="row-value">XAUUSD</strong></div>
    <div class="row"><span class="row-label">آخر تحديث</span><strong class="row-value" id="lastUpdate2">—</strong></div>
  </div>
</section>
</main>
<nav>
  <button class="active" data-page="home"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg><span>الرئيسية</span></button>
  <button data-page="chart"><svg viewBox="0 0 24 24"><path d="M7 3v4M7 17v4M17 3v2M17 15v6"/><rect x="5" y="7" width="4" height="10" rx="1"/><rect x="15" y="5" width="4" height="10" rx="1"/></svg><span>الشارت</span></button>
  <button data-page="journal"><svg viewBox="0 0 24 24"><path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/></svg><span>السجل</span></button>
  <button data-page="settings"><svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg><span>الإعدادات</span></button>
</nav>
<script>
function updateBaghdadClock(){
  const now = new Date();
  const timeEl = document.getElementById('baghdadTime'), dateEl = document.getElementById('baghdadDate');
  if (!timeEl || !dateEl) return;
  try {
    timeEl.textContent = new Intl.DateTimeFormat('en-GB', {
      timeZone:'Asia/Baghdad', hour:'2-digit', minute:'2-digit', second:'2-digit',
      hourCycle:'h23'
    }).format(now);
    dateEl.textContent = new Intl.DateTimeFormat('ar-IQ', {
      timeZone:'Asia/Baghdad', weekday:'long', day:'numeric', month:'long', year:'numeric'
    }).format(now);
  } catch(e) {
    timeEl.textContent = now.toLocaleTimeString();
    dateEl.textContent = 'توقيت محلي';
  }
}
const $ = id => document.getElementById(id);
const defaults = { tgToken: '', tgChatId: '', tgAuto: false };
let analysis = null;
let candles = [];
let chartTF = '5m';
const TF_SEC = { '5m': 300, '15m': 900, '1h': 3600 };
let liveCandle = null;
let staleCandles = [];
let liveQuote = null;
let lastMid = null;
let lastSignalKey = localStorage.getItem('lastSignalKey') || null;
let lastSignalTime = Number(localStorage.getItem('lastSignalTime') || 0);
let crosshairMode = false;
let crosshair = { active: false, x: 0, y: 0, price: null, time: null };
const chartView = { offsetX: 0, zoomX: 1, panY: 0 };
const gesture = { mode: null, startX: 0, startY: 0, startOffsetX: 0, startPanY: 0, startZoomX: 1, startDist: 0 };

function getSettings(){ try { return Object.assign({}, defaults, JSON.parse(localStorage.getItem('fvgSettings') || '{}')); } catch(e){ return Object.assign({}, defaults); } }
function loadSettings(){
  const s = getSettings();
  $('tgToken').value = s.tgToken || ''; $('tgChatId').value = s.tgChatId || '';
  $('tgAuto').checked = !!s.tgAuto;
}
function saveSettingsFn(){
  localStorage.setItem('fvgSettings', JSON.stringify({
    tgToken: $('tgToken').value.trim(),
    tgChatId: $('tgChatId').value.trim(),
    tgAuto: $('tgAuto').checked
  }));
  alert('تم حفظ الإعدادات');
  runAnalysis();
}
document.querySelectorAll('nav button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $(btn.dataset.page).classList.add('active');
    if (btn.dataset.page === 'chart') drawChart();
  };
});
$('saveSettingsBtn').onclick = saveSettingsFn;
$('chartReset').onclick = resetChartView;
document.querySelectorAll('#tfRow button').forEach(b => b.onclick = () => {
  chartTF = b.dataset.tf;
  document.querySelectorAll('#tfRow button').forEach(x => x.classList.toggle('active', x === b));
  $('chartTfLabel').textContent = b.textContent;
  candles = (analysis && analysis.candles && analysis.candles[chartTF]) || [];
  liveCandle = null; staleCandles = [];
  updateLiveCandle(); resetChartView();
  $('barCount').textContent = getDisplayCandles().length;
});
$('crossBtn').onclick = () => {
  crosshairMode = !crosshairMode;
  $('crossBtn').classList.toggle('active', crosshairMode);
  if (!crosshairMode) crosshair.active = false;
  drawChart();
};
$('clearBtn').onclick = () => { if (confirm('مسح كل السجل؟')) { localStorage.removeItem('fvgJournal'); renderJournal(); } };
$('tgTestBtn').onclick = async () => {
  const s = getSettings();
  if (!s.tgToken || !s.tgChatId) { alert('أدخل Token و Chat ID ثم احفظ'); return; }
  try {
    const r = await fetch('/api/telegram', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ token: s.tgToken, chat_id: s.tgChatId, text: '✅ اختبار ناجح من FVG v2' }) });
    const res = await r.json();
    if (res.error) throw new Error(res.error);
    alert('تم الإرسال ✓');
  } catch(e) { alert('فشل: ' + e.message); }
};

function resetChartView(){ chartView.offsetX = 0; chartView.zoomX = 1; chartView.panY = 0; drawChart(); }
function fmt(x){ return Number.isFinite(Number(x)) ? Number(x).toFixed(2) : '—'; }
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function ago(ts){ const s = Math.max(0, Math.round((Date.now()/1000) - ts)); if (s < 60) return s + ' ث'; if (s < 3600) return Math.round(s/60) + ' د'; return Math.round(s/3600) + ' س'; }

function updateLiveCandle(){
  if (!candles.length || !liveQuote) return;
  const mid = Number(liveQuote.mid);
  if (!Number.isFinite(mid) || mid <= 0) return;
  const intervalSec = TF_SEC[chartTF];
  const bucketSec = Math.floor(Math.floor(Date.now()/1000) / intervalSec) * intervalSec;
  const lastClosed = candles[candles.length - 1];
  if (liveCandle && liveCandle.time < bucketSec) {
    liveCandle.isLive = false; staleCandles.push(liveCandle); liveCandle = null;
  }
  staleCandles = staleCandles.filter(c => c.time > lastClosed.time);
  if (lastClosed.time >= bucketSec) { liveCandle = null; return; }
  if (liveCandle && liveCandle.time === bucketSec) {
    liveCandle.close = mid;
    if (mid > liveCandle.high) liveCandle.high = mid;
    if (mid < liveCandle.low) liveCandle.low = mid;
  } else {
    const openPrice = staleCandles.length ? staleCandles[staleCandles.length-1].close : lastClosed.close;
    liveCandle = { time: bucketSec, open: openPrice, high: Math.max(openPrice, mid), low: Math.min(openPrice, mid), close: mid, isLive: true };
  }
}
function getDisplayCandles(){
  if (!candles.length) return liveCandle ? [liveCandle] : [];
  const lastClosed = candles[candles.length - 1];
  const stale = staleCandles.filter(c => c.time > lastClosed.time);
  return candles.concat(stale).concat(liveCandle ? [liveCandle] : []);
}

let analysisBusy = false, analysisQueued = false;
async function runAnalysis(){
  if (analysisBusy) { analysisQueued = true; return; }
  analysisBusy = true;
  try {
    const s = getSettings();
    const qs = `rr=${s.rr}&buffer=${s.bufferMult}&capital=${s.capital}&risk=${s.riskPct}`;
    const r = await fetch('/api/analysis?' + qs, { cache: 'no-store' });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'فشل التحليل');
    analysis = d; candles = (d.candles && d.candles[chartTF]) || [];
    updateLiveCandle(); render(d); drawChart(); autoSaveSignal(d); checkTelegram(d);
    const now = new Date().toLocaleTimeString('ar-IQ');
    $('lastUpdate').textContent = 'آخر تحديث: ' + now;
    $('lastUpdate2').textContent = now;
  } catch(e) {
    $('signalBody').innerHTML = '<div class="empty-state"><span class="big-icon">✕</span><span style="color:var(--red)">تعذر: ' + esc(e.message) + '</span></div>';
  } finally {
    analysisBusy = false;
    if (analysisQueued) { analysisQueued = false; runAnalysis(); }
  }
}
async function refreshQuote(){
  try {
    const r = await fetch('/api/quote', { cache: 'no-store' });
    const q = await r.json();
    if (!r.ok || q.error) throw new Error(q.error || 'فشل السعر');
    liveQuote = q;
    const priceEl = $('price'); const mid = Number(q.mid);
    priceEl.textContent = fmt(q.mid);
    if (lastMid !== null && Number.isFinite(mid)) {
      if (mid > lastMid) { priceEl.classList.remove('tick-down'); priceEl.classList.add('tick-up'); }
      else if (mid < lastMid) { priceEl.classList.remove('tick-up'); priceEl.classList.add('tick-down'); }
      setTimeout(() => priceEl.classList.remove('tick-up','tick-down'), 220);
    }
    if (Number.isFinite(mid)) lastMid = mid;
    $('priceTime').textContent = q.timestamp || '—';
    $('bidAsk').textContent = 'Bid ' + fmt(q.bid) + ' · Ask ' + fmt(q.ask);
    $('spread').textContent = fmt(q.spread);
    const st = $('status');
    if (q.stale) { st.textContent = 'قديم'; st.classList.add('stale'); }
    else { st.textContent = 'مباشر'; st.classList.remove('stale'); }
    updateLiveCandle();
    if ($('chart').classList.contains('active')) drawChart();
  } catch(e) {
    $('status').textContent = 'انقطع'; $('status').classList.add('stale');
  }
}
function checkTelegram(d){
  const s = getSettings();
  if (!s.tgAuto || !s.tgToken || !s.tgChatId) return;
  if (!d.signal) { return; }
  const key = d.signal.side + '_' + d.signal.time;
  if (key === lastSignalKey && Date.now() - lastSignalTime < 15*60*1000) return;
  lastSignalKey = key; lastSignalTime = Date.now();
  localStorage.setItem('lastSignalKey', key);
  localStorage.setItem('lastSignalTime', String(lastSignalTime));
  const sideText = d.signal.side === 'BUY' ? 'شراء 🟢' : 'بيع 🔴';
  const msg = `<b>XAU/USD · ${sideText}</b>\n`
    + `دخول: <b>${fmt(d.signal.entry)}</b>\n`
    + `هدف أول: <b>${fmt(d.signal.target1)}</b>\n`
    + `هدف ثاني: <b>${fmt(d.signal.target2)}</b>`;
  fetch('/api/telegram', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ token: s.tgToken, chat_id: s.tgChatId, text: msg }) })
    .then(r => r.json()).then(res => { if (res.error) console.error(res.error); }).catch(e => console.error(e));
}
function render(d){
  if (!liveQuote) { $('price').textContent = fmt(d.last_price); $('priceTime').textContent = d.last_time || '—'; }
  const tb = $('trendBadge');
  const trendMap = { BULLISH:{label:'↑ صاعد',cls:'bullish'}, BEARISH:{label:'↓ هابط',cls:'bearish'}, NEUTRAL:{label:'→ محايد',cls:'neutral'}, UNKNOWN:{label:'؟',cls:'neutral'} };
  const tr = d.trends || {};
  const t = trendMap[tr['1h']] || trendMap.UNKNOWN;
  const ar = x => ({BULLISH:'↑',BEARISH:'↓',NEUTRAL:'→'})[x] || '؟';
  tb.textContent = 'H1 ' + ar(tr['1h']) + ' · M15 ' + ar(tr['15m']) + ' · M5 ' + ar(tr['5m']);
  tb.className = 'trend-badge ' + t.cls;
  $('atrVal').textContent = fmt(d.atr);
  $('sessionVal').textContent = ({asia:'آسيا (طوكيو/سيدني)',london:'لندن',newyork:'نيويورك',overlap:'تقاطع لندن ونيويورك'})[d.session] || d.session;
  $('trendM5').textContent = ({BULLISH:'↑',BEARISH:'↓',NEUTRAL:'→',UNKNOWN:'؟'})[d.trend_m5] || '—';
  $('barCount').textContent = getDisplayCandles().length;
  const mc = d.macd || {};
  ['1h','15m','5m'].forEach(k => {
    const m = mc[k], el = $('macd' + k);
    el.textContent = m ? ((m.hist >= 0 ? '▲ ' : '▼ ') + Number(m.hist).toFixed(2) + (m.cross ? ' ✦' : '')) : '—';
    el.style.color = m ? (m.hist >= 0 ? 'var(--green)' : 'var(--red)') : '';
  });
  const lv = d.levels || [];
  const sup = lv.filter(l => l.kind === 'support')[0], res = lv.filter(l => l.kind === 'resistance')[0];
  $('srSup').textContent = sup ? fmt(sup.price) + ' ×' + sup.strength : '—';
  $('srRes').textContent = res ? fmt(res.price) + ' ×' + res.strength : '—';
  renderFvgList(d.active_fvgs || []);
  renderLiquidity(d.sweeps || []);
  const card = $('signalCard'), badge = $('sideBadge'), body = $('signalBody');
  card.classList.remove('buy','sell');
  if (!d.signal) {
    badge.classList.remove('show');
    body.innerHTML = '<div class="empty-state"><span class="big-icon">◌</span><strong>لا توجد إشارة مؤكدة</strong><span>' + esc(d.message || 'في انتظار إعدادات أفضل') + '</span></div>' +
      (d.blocked || []).map(b => '<div class="blocked">⛔ ' + esc(b.side) + ' ' + esc(b.kind) + ' · ' + esc(b.score) + '/100 — ' + esc(b.reason) + '</div>').join('');
    return;
  }
  const s = d.signal;
  const isBuy = s.side === 'BUY';
  card.classList.add(isBuy ? 'buy' : 'sell');
  badge.className = 'side-badge show ' + (isBuy ? 'buy' : 'sell');
  badge.textContent = isBuy ? 'BUY' : 'SELL';
  body.innerHTML =
    '<div class="score-row"><span style="color:var(--muted)">قوة الإشارة</span>' +
    '<span class="score-badge ' + s.label + '">' + s.label.toUpperCase() + ' · ' + s.score + '/100</span></div>' +
    '<div class="reasons"><div class="label" style="margin-bottom:6px">أسباب الإشارة · ' + esc(s.kind || '') + ' ' + esc(s.tf || '') + '</div><ul>' +
    (s.reasons || []).map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>' +
    (s.warnings || []).map(w => '<div class="warn">⚠ ' + esc(w) + '</div>').join('') + '</div>';
}
function renderFvgList(fvgs){
  $('fvgCount').textContent = fvgs.length;
  const list = $('fvgList');
  if (!fvgs.length) { list.innerHTML = '<div class="empty-state">لا توجد فجوات نشطة حالياً</div>'; return; }
  fvgs.sort((a,b) => b.score - a.score);
  list.innerHTML = fvgs.map(f => {
    const isBull = (f.bias || f.type) === 'bullish';
    const icon = isBull ? '↑' : '↓';
    const cls = isBull ? 'bull' : 'bear';
    return '<div class="fvg-item ' + cls + ' ' + f.label + '">' +
      '<span class="fvg-type" style="color:' + (isBull ? 'var(--green)' : 'var(--red)') + '">' + icon + '</span>' +
      '<div class="fvg-info"><div>' + (f.kind || 'FVG') + ' ' + f.tf + ' · ' + fmt(f.bottom) + ' — ' + fmt(f.top) + '</div>' +
      '<div class="fvg-meta">' + ago(f.formed_at) + ' · ' + f.session + ' · ' + f.status + '</div></div>' +
      '<span class="fvg-score ' + f.label + '">' + f.score + '</span>' +
    '</div>';
  }).join('');
}
function renderLiquidity(sweeps){
  const list = $('liquidityList');
  if (!list) return;
  if (!sweeps || !sweeps.length) {
    list.innerHTML = '<div class="empty-state">لا يوجد سحب سيولة مؤكد حالياً</div>';
    return;
  }
  list.innerHTML = sweeps.slice(0, 8).map(sw => {
    const isBuy = sw.side === 'BUY';
    const title = isBuy ? 'سحب سيولة من القيعان' : 'سحب سيولة من القمم';
    return '<div class="liquidity-item ' + (isBuy ? 'buy' : 'sell') + '">' +
      '<div><div class="liq-side">' + (isBuy ? '↑ ' : '↓ ') + title + '</div>' +
      '<div class="liq-meta">' + esc(sw.tf || '') + ' · مستوى السيولة</div></div>' +
      '<strong>' + fmt(sw.level) + '</strong></div>';
  }).join('');
}
function drawChart(){
  const c = $('chartCanvas');
  if (!c) return;
  const rect = c.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const dpr = window.devicePixelRatio || 1;
  c.width = rect.width * dpr; c.height = rect.height * dpr;
  const ctx = c.getContext('2d'); ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  ctx.clearRect(0, 0, w, h);
  const display = getDisplayCandles();
  if (!display.length) { ctx.fillStyle = '#7d8ba8'; ctx.font = '13px -apple-system'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText('لا توجد بيانات', w/2, h/2); return; }
  const padL = 10, padR = 60, padT = 22, padB = 25;
  const chartW = w - padL - padR, chartH = h - padT - padB;
  const total = display.length;
  const visibleCount = Math.max(15, Math.min(total, Math.round(60 / chartView.zoomX)));
  const endIdx = Math.max(visibleCount, total - chartView.offsetX);
  const startIdx = Math.max(0, endIdx - visibleCount);
  const visible = display.slice(startIdx, endIdx);
  if (!visible.length) return;
  let min = Math.min(...visible.map(x => x.low)), max = Math.max(...visible.map(x => x.high));
  if (liveQuote && Number.isFinite(Number(liveQuote.mid))) {
    const m = Number(liveQuote.mid); if (m < min) min = m; if (m > max) max = m;
  }
  const baseRange = max - min || 1;
  min -= baseRange * 0.04; max += baseRange * 0.04;
  const ppP = (max - min) / chartH;
  min += chartView.panY * ppP; max += chartView.panY * ppP;
  const finalRange = max - min || 1;
  // Grid
  ctx.strokeStyle = 'rgba(255,255,255,.05)'; ctx.fillStyle = '#7d8ba8';
  ctx.font = '10px -apple-system'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const y = padT + (chartH * i) / 4;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText((max - finalRange * i / 4).toFixed(2), w - padR + 6, y);
  }
  // FVG rectangles
  if (analysis && analysis.active_fvgs) {
    analysis.active_fvgs.forEach(fvg => {
      const yTop = padT + ((max - fvg.top) / finalRange) * chartH;
      const yBot = padT + ((max - fvg.bottom) / finalRange) * chartH;
      if (yBot < padT || yTop > padT + chartH) return;
      const isBull = (fvg.bias || fvg.type) === 'bullish';
      const alpha = fvg.label === 'high' ? 0.22 : fvg.label === 'medium' ? 0.14 : 0.08;
      ctx.fillStyle = (isBull ? 'rgba(61,220,151,' : 'rgba(255,85,102,') + alpha + ')';
      const y = Math.min(yTop, yBot), hh = Math.max(2, Math.abs(yBot - yTop));
      ctx.fillRect(padL, y, chartW, hh);
      if (fvg.kind === 'IFVG') { ctx.setLineDash([4,3]); ctx.strokeStyle = isBull ? 'rgba(61,220,151,.7)' : 'rgba(255,85,102,.7)'; ctx.lineWidth = 1; ctx.strokeRect(padL, y, chartW, hh); ctx.setLineDash([]); }
      if (fvg.label === 'high') {
        ctx.strokeStyle = (isBull ? 'rgba(61,220,151,.6)' : 'rgba(255,85,102,.6)');
        ctx.lineWidth = 1; ctx.strokeRect(padL, y, chartW, hh);
      }
    });
  }
  // Support / resistance
  (analysis && analysis.levels || []).forEach(l => {
    const y = padT + ((max - l.price) / finalRange) * chartH;
    if (y < padT || y > padT + chartH) return;
    const isSup = l.kind === 'support';
    ctx.strokeStyle = isSup ? 'rgba(61,220,151,.55)' : 'rgba(255,85,102,.55)';
    ctx.lineWidth = 1; ctx.setLineDash([6,4]);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = isSup ? '#3ddc97' : '#ff5566'; ctx.font = '9px -apple-system'; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    ctx.fillText((isSup ? 'S ' : 'R ') + Number(l.price).toFixed(2) + ' ×' + l.strength, padL + 4, y - 2);
  });
  ctx.textBaseline = 'middle';
  // Candles
  const step = chartW / visible.length;
  const cw = Math.max(2, step * 0.6);
  let liveX = null;
  visible.forEach((b, i) => {
    const x = padL + i * step + step/2;
    const yOpen = padT + ((max - b.open) / finalRange) * chartH;
    const yClose = padT + ((max - b.close) / finalRange) * chartH;
    const yHigh = padT + ((max - b.high) / finalRange) * chartH;
    const yLow = padT + ((max - b.low) / finalRange) * chartH;
    const isUp = b.close >= b.open;
    const color = isUp ? '#3ddc97' : '#ff5566';
    ctx.strokeStyle = color; ctx.fillStyle = color;
    ctx.beginPath(); ctx.moveTo(x, yHigh); ctx.lineTo(x, yLow); ctx.stroke();
    const top = Math.min(yOpen, yClose);
    ctx.fillRect(x - cw/2, top, cw, Math.max(1, Math.abs(yClose - yOpen)));
    if (b.isLive) liveX = x;
  });
  // Live price line
  if (liveQuote && Number.isFinite(Number(liveQuote.mid))) {
    const m = Number(liveQuote.mid);
    const yMid = padT + ((max - m) / finalRange) * chartH;
    if (yMid >= padT && yMid <= padT + chartH) {
      ctx.strokeStyle = 'rgba(244,202,105,.9)'; ctx.lineWidth = 1.2; ctx.setLineDash([2,3]);
      ctx.beginPath(); ctx.moveTo(padL, yMid); ctx.lineTo(w - padR, yMid); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = '#f4ca69'; ctx.font = 'bold 10px -apple-system';
      ctx.fillText(m.toFixed(2), w - padR + 6, yMid); ctx.lineWidth = 1;
    }
  }
  if (liveX !== null) {
    ctx.fillStyle = '#f4ca69'; ctx.beginPath(); ctx.arc(liveX, padT + chartH + 10, 2.5, 0, Math.PI*2); ctx.fill();
  }
  // Crosshair
  if (crosshair.active) {
    const x = crosshair.x, y = crosshair.y;
    ctx.strokeStyle = 'rgba(255,255,255,.4)'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + chartH); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.setLineDash([]);
    const price = max - ((y - padT) / chartH) * finalRange;
    ctx.fillStyle = 'rgba(5,7,15,.95)'; ctx.strokeStyle = 'rgba(244,202,105,.8)';
    ctx.fillRect(w - padR + 2, y - 8, 56, 16);
    ctx.strokeRect(w - padR + 2, y - 8, 56, 16);
    ctx.fillStyle = '#f4ca69'; ctx.font = 'bold 10px -apple-system'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText(price.toFixed(2), w - padR + 6, y);
  }
}
function getTouchDist(t1, t2){ const dx = t2.clientX-t1.clientX, dy = t2.clientY-t1.clientY; return Math.sqrt(dx*dx+dy*dy); }
function panBy(dx, dy){
  const c = $('chartCanvas'); const rect = c.getBoundingClientRect();
  const padL = 10, padR = 60, chartW = rect.width - padL - padR;
  const display = getDisplayCandles(); const total = Math.max(1, display.length);
  const visibleCount = Math.max(15, Math.min(total, Math.round(60 / chartView.zoomX)));
  const step = chartW / visibleCount;
  chartView.offsetX = gesture.startOffsetX + Math.round(dx / step);
  const maxOffset = Math.max(0, total - 15);
  chartView.offsetX = Math.max(0, Math.min(maxOffset, chartView.offsetX));
  chartView.panY = gesture.startPanY + dy;
}
function initChartGestures(){
  const c = $('chartCanvas');
  c.addEventListener('touchstart', e => {
    e.preventDefault();
    if (crosshairMode && e.touches.length === 1) { setCrosshair(e.touches[0], c); return; }
    if (e.touches.length === 1) {
      gesture.mode = 'pan'; gesture.startX = e.touches[0].clientX; gesture.startY = e.touches[0].clientY;
      gesture.startOffsetX = chartView.offsetX; gesture.startPanY = chartView.panY;
    } else if (e.touches.length === 2) {
      gesture.mode = 'pinch'; gesture.startDist = getTouchDist(e.touches[0], e.touches[1]); gesture.startZoomX = chartView.zoomX;
    }
  }, { passive: false });
  c.addEventListener('touchmove', e => {
    e.preventDefault();
    if (crosshairMode && e.touches.length === 1) { setCrosshair(e.touches[0], c); return; }
    if (gesture.mode === 'pan' && e.touches.length === 1) {
      const dx = e.touches[0].clientX - gesture.startX, dy = e.touches[0].clientY - gesture.startY;
      panBy(dx, dy); drawChart();
    } else if (gesture.mode === 'pinch' && e.touches.length === 2) {
      const dist = getTouchDist(e.touches[0], e.touches[1]);
      if (gesture.startDist > 0) {
        chartView.zoomX = Math.max(0.25, Math.min(10, gesture.startZoomX * (dist / gesture.startDist)));
        drawChart();
      }
    }
  }, { passive: false });
  c.addEventListener('touchend', e => {
    if (e.touches.length === 0) gesture.mode = null;
    else if (e.touches.length === 1 && !crosshairMode) {
      gesture.mode = 'pan'; gesture.startX = e.touches[0].clientX; gesture.startY = e.touches[0].clientY;
      gesture.startOffsetX = chartView.offsetX; gesture.startPanY = chartView.panY;
    }
  }, { passive: false });
  c.addEventListener('mousedown', e => {
    if (crosshairMode) { setCrosshair(e, c); return; }
    gesture.mode = 'pan'; gesture.startX = e.clientX; gesture.startY = e.clientY;
    gesture.startOffsetX = chartView.offsetX; gesture.startPanY = chartView.panY;
  });
  window.addEventListener('mousemove', e => {
    if (crosshairMode && crosshair.active) { setCrosshair(e, c); return; }
    if (gesture.mode !== 'pan') return;
    panBy(e.clientX - gesture.startX, e.clientY - gesture.startY); drawChart();
  });
  window.addEventListener('mouseup', () => { gesture.mode = null; });
  c.addEventListener('wheel', e => {
    e.preventDefault();
    chartView.zoomX = Math.max(0.25, Math.min(10, chartView.zoomX * (e.deltaY > 0 ? 0.9 : 1.1)));
    drawChart();
  }, { passive: false });
}
function setCrosshair(touch, c){
  const rect = c.getBoundingClientRect();
  const x = touch.clientX - rect.left, y = touch.clientY - rect.top;
  crosshair.active = true; crosshair.x = x; crosshair.y = y;
  drawChart();
}
function loadJournal(){
  try {
    const j = JSON.parse(localStorage.getItem('fvgJournal') || '[]');
    return Array.isArray(j) ? j.filter(x => x && x.signal) : [];
  } catch(e){ return []; }
}
function autoSaveSignal(d){
  if (!d || !d.signal) return;
  const sig = d.signal;
  const key = [sig.side || '', sig.time || '', sig.entry || '', sig.target1 || '', sig.target2 || ''].join('_');
  if (localStorage.getItem('lastJournalSignalKey') === key) return;
  const j = loadJournal();
  j.unshift({ at: new Date().toISOString(), signal: sig, trend: d.trends || d.trend || {} });
  localStorage.setItem('fvgJournal', JSON.stringify(j.slice(0, 100)));
  localStorage.setItem('lastJournalSignalKey', key);
  renderJournal();
}
function renderJournal(){
  const j = loadJournal();
  const list = $('journalList');
  if (!j.length) { list.innerHTML = '<div class="empty-state"><span class="big-icon">▤</span>لا توجد إشارات</div>'; return; }
  list.innerHTML = j.map((x, i) => {
    const s = x.signal; const isBuy = s.side === 'BUY';
    const dt = new Date(x.at);
    const timeStr = dt.toLocaleString('ar-IQ', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
    return '<div class="journal-item">' +
      '<span class="j-side ' + (isBuy?'buy':'sell') + '">' + esc(s.side) + '</span>' +
      '<div class="j-body"><div>دخول <strong>' + fmt(s.entry) + '</strong> · وقف <strong>' + fmt(s.stop) + '</strong> · هدف <strong>' + fmt(s.target) + '</strong></div>' +
      '<div class="j-time">' + timeStr + ' · جودة ' + esc(s.score) + '</div></div>' +
      '<button class="j-del" data-idx="' + i + '">✕</button></div>';
  }).join('');
  list.querySelectorAll('.j-del').forEach(b => b.onclick = () => { deleteJournal(Number(b.dataset.idx)); });
}
function deleteJournal(i){ const j = loadJournal(); j.splice(i, 1); localStorage.setItem('fvgJournal', JSON.stringify(j)); renderJournal(); }

loadSettings(); renderJournal(); updateBaghdadClock(); setInterval(updateBaghdadClock, 1000); initChartGestures(); refreshQuote(); runAnalysis();
setInterval(refreshQuote, 1500);
setInterval(runAnalysis, 8000);
window.addEventListener('resize', () => { if ($('chart').classList.contains('active')) drawChart(); });
</script>
</body>
</html>
'''

# ───────────────────────── HTTP server ──────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass            # browser closed the connection - nothing to do

    def send_html(self):
        raw = HTML.replace("__APP_NAME__", APP_NAME).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass            # browser closed the connection - nothing to do

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw)
        except Exception as e:
            self.send_json(400, {"error": "Invalid body: " + str(e)}); return
        if self.path.startswith("/api/telegram"):
            token = (payload.get("token") or "").strip()
            chat_id = str(payload.get("chat_id") or "").strip()
            text = (payload.get("text") or "").strip()
            if not token or not chat_id or not text:
                self.send_json(400, {"error": "Missing token/chat_id/text"}); return
            try:
                send_telegram(token, chat_id, text); self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(502, {"error": str(e)})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        try:
            if self.path.startswith("/api/quote"):
                self.send_json(200, fetch_live_quote())
            elif self.path.startswith("/api/analysis"):
                qs = urllib.parse.urlparse(self.path).query
                p = urllib.parse.parse_qs(qs)
                def num(name, default):
                    try:
                        v = float(p.get(name, [default])[0])
                    except (TypeError, ValueError):
                        return default
                    return v if math.isfinite(v) else default
                self.send_json(200, analyze())
            elif self.path == "/" or self.path.startswith("/index"):
                self.send_html()
            else:
                self.send_response(404); self.end_headers()
        except Exception as e:
            log("GET error:", e)
            self.send_json(502, {"error": str(e)})


def main():
    load_state()
    server, port = None, PORT
    for port in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError as e:
            log(f"port {port} busy ({e}), trying next...")
    if server is None:
        print(f"Could not start: ports {PORT}-{PORT + 9} are busy. Stop the previous run in Pyto and retry.")
        return
    url = f"http://{HOST}:{port}/"
    print("=" * 55)
    print(APP_NAME + " — XAU/USD analysis app (v3)")
    print("Open: " + url)
    print("Keep Pyto running in background.")
    print("=" * 55)
    if os.environ.get("AURUM_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        persist_state(force=True)
        server.server_close()


if __name__ == "__main__":
    main()