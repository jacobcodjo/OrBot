"""Bot or (XAUUSD) : 5 stratégies price action, données Deriv, alertes Telegram, suivi des trades.

Stratégies (détail dans le README, section Stratégies) :
  S1 Pullback (structure + Fibonacci) · S2 Sweep + CHoCH · S3 Figures & Retournement · S4 CRT
Un passage = un scan (à déclencher toutes les 5 minutes par un cron externe, voir README.md).
"""
import json, os, random, sys, time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import websocket  # websocket-client

import charts
import guard as G
import strategies as S
import tracker as T
import setups as SU

APP_ID = os.getenv("DERIV_APP_ID") or "1089"
# 1) endpoint public Deriv (sans authentification), 2) ancien endpoint en secours.
# On peut forcer un endpoint avec la variable d'environnement DERIV_WS_URL.
WS_URLS = [u for u in (
    os.getenv("DERIV_WS_URL"),
    "wss://api.derivws.com/trading/v1/options/ws/public",
    f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
) if u]
UA = "Mozilla/5.0 (compatible; gold-bot/1.0)"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

GOLD = "frxXAUUSD"
SYMBOLS = [GOLD]                       # ce bot ne suit que l'or (XAUUSD)
BOT = "OR"
SPREAD_DEFAULT = 0.015                    # spread supposé de l'or, en % du prix (estimation ; variable SPREAD_PCT pour le remplacer)


def spread_pct(sym=GOLD):
    """Spread supposé de l'or, en % du prix : variable SPREAD_PCT (ex. ton spread Exness), sinon l'estimation par défaut."""
    try:
        if os.getenv("SPREAD_PCT"):
            return float(os.environ["SPREAD_PCT"])
    except ValueError:
        pass
    return SPREAD_DEFAULT


def canon_codes(codes, known_only=False):
    """Codes de stratégie sans doublon, dans l'ordre ; avec known_only, seuls les codes connus sont gardés."""
    out = []
    for c in codes:
        if c not in out and (c in S.STRAT_LABELS or not known_only):
            out.append(c)
    return out


ENTRY_MODES = {"S1": "limit", "S2": "limit", "S3": "limit", "S4": "limit", "S9": "limit"}   # défaut : ordres limites partout
for _p in (os.getenv("ENTRY_MODES") or "").split(","):                         # ex. ENTRY_MODES="S2:market,S3:market"
    _k, _, _v = _p.partition(":")
    _k = _k.strip().upper()
    if _k in ENTRY_MODES and _v.strip().lower() in ("limit", "market"):
        ENTRY_MODES[_k] = _v.strip().lower()
ENTRY_MODE = (os.getenv("ENTRY_MODE") or "auto").strip().lower()   # auto : ENTRY_MODES ; market ou limit : force toutes les stratégies


def entry_modes():
    """Mode d'entrée de chaque stratégie, selon ENTRY_MODE (auto / market / limit) et ENTRY_MODES."""
    return dict(ENTRY_MODES) if ENTRY_MODE == "auto" else {k: ENTRY_MODE for k in ENTRY_MODES} \
        if ENTRY_MODE in ("market", "limit") else dict(ENTRY_MODES)



def apply_cost(t, spread=None):
    """Retire le coût du spread (en R) du résultat d'un trade clôturé, une seule fois."""
    if t.get("r") is None or t.get("cost_r") is not None:
        return
    risk = abs(t["entry"] - t["sl"])
    if risk <= 0:
        return
    cost = (spread if spread is not None else spread_pct(t["sym"])) / 100 * t["entry"] / risk
    t["cost_r"], t["r"] = round(cost, 3), round(t["r"] - cost, 3)


def name_of(sym):
    return sym[3:] if sym.startswith("frx") else sym


def dec_of(sym):
    return 2                           # or : 2 décimales


def parse_strategies(text):
    """'S1,S2' -> ['S1', 'S2'] ; vide ou absent -> toutes les stratégies (S1, S2, S3, S4 ; GitHub transmet une chaîne vide si la variable n'existe pas)."""
    return canon_codes([x.strip().upper() for x in (text or "").split(",") if x.strip()]) or ["S1", "S2", "S3", "S4", "S9"]


ENABLED = parse_strategies(os.getenv("STRATEGIES"))


def parse_confluence(text, default=60.0):
    """Fenêtre de confluence en minutes. Vide, absent ou invalide -> 60 ; négatif ou 0 -> désactivée (0)."""
    try:
        v = float(str(text).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default
    return max(0.0, v)


CONFLUENCE_MIN = parse_confluence(os.getenv("CONFLUENCE_MIN"))   # minutes ; 0 = pas d'étiquette de confluence
LABEL_CODE = {v: k for k, v in S.STRAT_LABELS.items()}
GRAN = S.GRAN
COUNTS = {"M5": 300, "M15": 400}         # bougies téléchargées à chaque scan
COUNT_H1 = 2500                          # H1 : sert aussi à reconstruire H4 et D1 (17h New York), ~100 jours
STALE_DATA = 3 * GRAN["M5"]              # données plus vieilles que 15 min : marché fermé ou flux coupé
TRADE_TTL = 3600                         # durée de validité affichée (ordres au marché : sans effet)

MIN_INTERVAL = 0.6       # s entre deux requêtes (throttle)
MAX_RETRIES = 6
CACHE_FILE = "cache.json"
STATE_FILE = "state.json"
NY = ZoneInfo("America/New_York")


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# --------------------------------------------------------------------------
# Client Deriv robuste : throttle + backoff exponentiel + reconnexion auto
# --------------------------------------------------------------------------
class Deriv:
    def __init__(self):
        self.ws = None
        self.last = 0.0
        self.req_id = 0

    def _connect(self):
        self.close()
        for i in range(MAX_RETRIES):
            url = WS_URLS[i % len(WS_URLS)]
            name = url.split("?")[0]
            try:
                self.ws = websocket.create_connection(url, timeout=20, header=[f"User-Agent: {UA}"])
                print(f"Connecté à {name}")
                return
            except Exception as e:
                self._sleep_backoff(i, f"connexion {name}: {str(e)[:60]}")
        raise RuntimeError("Impossible de se connecter à Deriv")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    @staticmethod
    def _sleep_backoff(attempt, why):
        delay = min(60, 2 ** attempt) + random.uniform(0, 1)  # jitter
        print(f"[retry {attempt+1}] {why} -> pause {delay:.1f}s")
        time.sleep(delay)

    def request(self, payload):
        for attempt in range(MAX_RETRIES):
            try:
                if self.ws is None:
                    self._connect()
                wait = MIN_INTERVAL - (time.time() - self.last)
                if wait > 0:
                    time.sleep(wait)
                self.req_id += 1
                payload["req_id"] = self.req_id
                self.ws.send(json.dumps(payload))
                self.last = time.time()
                while True:  # ignore les messages sans rapport
                    msg = json.loads(self.ws.recv())
                    if msg.get("req_id") == self.req_id:
                        break
                err = msg.get("error")
                if err:
                    code = err.get("code")
                    if code in ("RateLimit", "RateLimitExceeded"):
                        self._sleep_backoff(attempt + 2, "rate limit")
                        continue
                    if code in ("InvalidSymbol", "MarketIsClosed"):
                        print(f"Ignoré ({code}): {payload.get('ticks_history')}")
                        return None  # marché fermé / symbole indisponible
                    raise RuntimeError(f"{code}: {err.get('message')}")
                return msg
            except (websocket.WebSocketException, ConnectionError, OSError) as e:
                self.close()  # forcera une reconnexion propre
                self._sleep_backoff(attempt, f"socket: {e}")
        return None

    def candles(self, symbol, gran, count=300, end="latest"):
        msg = self.request({"ticks_history": symbol, "style": "candles", "granularity": gran,
                            "count": count, "end": end})
        return msg.get("candles") if msg else None


def get_candles(api, cache, symbol, gran, force=False, count=300):
    """Cache : on ne re-télécharge que si la dernière bougie est clôturée (ou si force)."""
    key = f"{symbol}_{gran}"
    c = cache.get(key)
    if c and not force and time.time() < c[-1]["epoch"] + gran:
        return c
    fresh = api.candles(symbol, gran, count=count)
    if fresh:
        cache[key] = fresh
        return fresh
    return c


def aggregate_ny(h1, hours):
    """Regroupe des H1 en bougies de `hours` heures ouvrant à 17h New York (D1 = 24, H4 = 4)."""
    out = {}
    for c in h1:
        dt = datetime.fromtimestamp(c["epoch"], NY)
        h = (dt.hour - 17) % 24
        start = int((dt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h % hours)).timestamp())
        b = out.get(start)
        if b is None:
            out[start] = {"epoch": start, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
    return list(out.values())


def tf_candles(api, cache, sym, tf):
    """Bougies d'un timeframe ; la dernière est en formation. M5 et M15 sont toujours rafraîchis."""
    if tf in ("H1", "H4", "D1"):
        h1 = get_candles(api, cache, sym, GRAN["H1"], count=COUNT_H1)
        if not h1 or tf == "H1":
            return h1
        return aggregate_ny(h1, 24 if tf == "D1" else 4)
    return get_candles(api, cache, sym, GRAN[tf], force=True, count=COUNTS[tf])


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def alert_clock(epoch):
    """« 08h15 Bénin » : heure locale affichée dans les messages. Variable ALERT_FUSEAU (défaut Africa/Porto-Novo ;
    none pour désactiver) et ALERT_FUSEAU_NOM pour le libellé. Retourne None si désactivé ou fuseau invalide."""
    name = (os.getenv("ALERT_FUSEAU") or "Africa/Porto-Novo").strip()
    if name.lower() in ("none", "non", "off", "0"):
        return None
    try:
        tz = ZoneInfo(name)
    except Exception:
        return None
    dt = datetime.fromtimestamp(epoch, tz)
    label = os.getenv("ALERT_FUSEAU_NOM") or {"Africa/Porto-Novo": "Bénin"}.get(name, dt.tzname())
    return f"{dt:%Hh%M} {label}"


def fmt(x, dec):
    return f"{x:.{dec}f}"


def code(x, dec):
    return f"<code>{fmt(x, dec)}</code>"


def tg_api(method, payload=None, files=None):
    """Appel à l'API Telegram ; retourne le JSON de la réponse ou None en cas d'échec."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    try:
        if files:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, files=files, timeout=30)
        else:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=15)
        if not r.ok:
            print(f"Telegram {method} erreur {r.status_code}: {r.text[:150]}")
            return None
        return r.json()
    except Exception as e:
        print(f"Telegram injoignable ({method}): {str(e)[:100]}")
        return None


def notify(text, html=False, buttons=None):
    """Envoie un message ; retourne son identifiant (None si l'envoi échoue ou sans secrets Telegram)."""
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID vide -> message NON envoyé :")
        print(text)
        return None
    payload = {"chat_id": TG_CHAT, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    r = tg_api("sendMessage", payload)
    return r["result"]["message_id"] if r and r.get("ok") else None


def notify_photo(path, caption, html=True, buttons=None):
    """Envoie une image avec légende (1024 caractères au plus) ; retourne l'identifiant du message ou None."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    payload = {"chat_id": TG_CHAT, "caption": caption}
    if html:
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    with open(path, "rb") as f:
        r = tg_api("sendPhoto", payload, files={"photo": f})
    return r["result"]["message_id"] if r and r.get("ok") else None


def ratio_txt(d, entry, sl, tp):
    """Ratio gain/risque d'une cible, ex. '   (1:2.0)'."""
    risk = abs(entry - sl)
    gain = (tp - entry) if d == "BUY" else (entry - tp)
    if risk <= 0:
        return ""
    return "   (dépassé)" if gain <= 0 else f"   (1:{gain / risk:.1f})"


def mgmt_line(sig):
    """Règle de gestion annoncée dans l'alerte (reflète les réglages de tracker.py)."""
    p1, p2 = T.PARTIAL_AT_TP1, T.PARTIAL_AT_TP2
    has3 = sig.get("tp3") is not None
    parts = []
    if p1:
        parts.append(f"{int(p1 * 100)} % à TP1" + (" (SL à l'entrée)" if T.BE_AT_TP1 else ""))
    if has3 and p2:
        parts.append(f"{int(p2 * 100)} % à TP2" + (" (SL à TP1)" if T.LOCK_AT_TP2 else ""))
    rest = round((1 - p1 - (p2 if has3 else 0)) * 100)
    if rest > 0:
        parts.append(f"{rest} % vers " + ("TP3" if has3 and p2 else "TP2"))
    if not parts:
        return None
    out = "Gestion : " + ", ".join(parts)
    exits = (["structure cassée"] if T.STRUCT_EXIT and sig.get("inval") is not None else []) + \
            ([f"TP1 non atteint en {T.TIME_STOP_BARS} bougies"] if T.TIME_STOP_BARS else [])
    return out + (". Sortie si " + " ou ".join(exits) if exits else "")


ALERT_FORMATS = ("complet", "compact", "minimal", "contexte", "essentiel")


def alert_format():
    """Format des alertes, réglé par la variable ALERT_FORMAT (défaut : essentiel)."""
    fmt_name = (os.getenv("ALERT_FORMAT") or "essentiel").strip().lower()
    return fmt_name if fmt_name in ALERT_FORMATS else "essentiel"


def sig_clock(sig):
    return alert_clock(sig["ts"]) if sig.get("ts") else None


def limit_validity(sig):
    """« valable jusqu'à 10h15 Bénin » (ou « valable 120 min » sans fuseau d'affichage)."""
    until = alert_clock(sig["ts"] + sig["ttl"]) if sig.get("ts") else None
    return f"valable jusqu'à {until}" if until else f"valable {round(sig['ttl'] / 60)} min"


def age_txt(sig):
    a = sig.get("age_min")
    if a is None:
        return None
    return "à l'instant" if a < 1 else f"il y a {a:.0f} min"


def is_limit(sig):
    return sig.get("otype") not in (None, "BUY", "SELL")


def entry_txt(sig, c):
    """« 2652.40 (marché) » ou « BUY LIMIT 2650.10 (prix 2652.40) »."""
    if is_limit(sig):
        return f"{sig['otype']} {c(sig['entry'])} (prix {c(sig['price_now'])})"
    return f"{c(sig['entry'])} (marché)"


def rr_txt(d, entry, sl, tp):
    risk = abs(entry - sl)
    gain = ((tp - entry) if d == "BUY" else (entry - tp)) / risk if risk > 0 else 0
    return f"1:{gain:.1f}".replace(".0", "")


def _tp_line(sig, c, with_rr=True):
    d = sig["dir"]
    return " · ".join(f"{n} {c(sig[k])}" + (f" ({rr_txt(d, sig['entry'], sig['sl'], sig[k])})" if with_rr else "")
                      for n, k in (("TP1", "tp1"), ("TP2", "tp2"), ("TP3", "tp3")) if sig.get(k) is not None)


def message_complet(sig, tid):
    d, dec = sig["dir"], sig["dec"]
    c = lambda v: code(v, dec)
    tps = [("TP1", sig["tp1"]), ("TP2", sig["tp2"]), ("TP3", sig["tp3"])]
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} {d} · {sig['name']} · {sig['ltf']} · {sig['label']}",
             f"Session : {sig['session']}" + (f" · {sig_clock(sig)}" if sig_clock(sig) else "")
             + (f" · signal {age_txt(sig)}" if age_txt(sig) else ""), "",
             "────────────────", f"Prix actuel  {c(sig.get('price_now', sig['entry']))}", "────────────────", "",
             (f"Ordre  {sig['otype']}  {c(sig['entry'])}  ({limit_validity(sig)})" if is_limit(sig)
              else f"Entrée  {c(sig['entry'])}  (au marché)"), f"SL  {c(sig['sl'])}"]
    lines += [f"{n}  {c(v)}{ratio_txt(d, sig['entry'], sig['sl'], v)}" for n, v in tps if v is not None]
    lines += [""] + sig["details"] + ([f"📌 {sig['entry_note']}"] if sig.get("entry_note") else []) + sig.get("trend_lines", [])
    if sig.get("inval") is not None and T.STRUCT_EXIT:
        lines.append(f"Invalidation  {c(sig['inval'])}  (clôture au-delà = sortie)")
    cancel = ("Annule l'ordre si TP1 est atteint avant l'entrée, si une bougie clôture au-delà du stop, "
              "ou après le délai." if is_limit(sig) else None)
    extra = [x for x in (cancel, mgmt_line(sig)) if x]
    if extra:
        lines += [""] + extra
    return "\n".join(lines) + (f"\n\nRéf. #{tid}" if tid else "")


def message_compact(sig, tid):
    d, c = sig["dir"], (lambda v: code(v, sig["dec"]))
    when = f" · {sig_clock(sig)}" if sig_clock(sig) else ""
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} {d} · {sig['name']} · {sig['ltf']} · {sig['label']} · {sig['session']}{when}", "",
             f"Entrée  {entry_txt(sig, c)}", f"SL  {c(sig['sl'])}", _tp_line(sig, c), "",
             f"✅ {sig['short']}"]
    if sig.get("trend_short"):
        lines.append(sig["trend_short"])
    tail = " · ".join(x for x in (f"Réf. #{tid}" if tid else None,
                                   f"signal {age_txt(sig)}" if age_txt(sig) else None) if x)
    return "\n".join(lines + ([""] + [tail] if tail else []))


def message_minimal(sig, tid):
    d, c = sig["dir"], (lambda v: code(v, sig["dec"]))
    tps = " / ".join(c(sig[k]) for k in ("tp1", "tp2", "tp3") if sig.get(k) is not None)
    tail = " · ".join(x for x in (f"#{tid}" if tid else None,) if x)
    return "\n".join([f"{'🟢' if d == 'BUY' else '🔴'} {d} {sig['name']} {sig['ltf']} · {sig['label']}"
                       + (f" · {sig_clock(sig)}" if sig_clock(sig) else "")
                       + (f" · ⚡{'+'.join(sig['confluence'])}" if sig.get("confluence") else "")
                      ,
                      f"{sig['otype'] + ' ' if is_limit(sig) else ''}{c(sig['entry'])} · SL {c(sig['sl'])} · TP {tps}"]
                     + ([tail] if tail else []))


def message_essentiel(sig, tid):
    """3 lignes : type d'ordre, actif, stratégie (avec la confluence), heure ; niveaux ; validité et numéro."""
    d, c = sig["dir"], (lambda v: code(v, sig["dec"]))
    names = [sig["label"]] + [S.STRAT_LABELS.get(x, x) for x in (sig.get("confluence") or [])]
    strat = ("⚡ " if len(names) > 1 else "") + " + ".join(names)
    tps = " / ".join(c(sig[k]) for k in ("tp1", "tp2", "tp3") if sig.get(k) is not None)
    entry = c(sig["entry"]) if is_limit(sig) else f"Marché {c(sig['entry'])}"
    l1 = (f"{'🟢' if d == 'BUY' else '🔴'} {sig['otype'] if is_limit(sig) else d} · {sig['name']} {sig['ltf']} · {strat}"
          + (f" · {sig_clock(sig)}" if sig_clock(sig) else ""))
    l2 = f"{entry} · SL {c(sig['sl'])} · TP {tps}"
    valid = limit_validity(sig) if is_limit(sig) else None
    age = f"signal {age_txt(sig)}" if (sig.get("age_min") or 0) >= 3 else None    # alerte tardive : on le dit
    l3 = " · ".join(x for x in ((valid[0].upper() + valid[1:]) if valid else None, age, f"#{tid}" if tid else None) if x)
    return "\n".join([l1, l2] + ([l3] if l3 else []))


def message_contexte(sig, tid):
    d, c = sig["dir"], (lambda v: code(v, sig["dec"]))
    risk = abs(sig["entry"] - sig["sl"])
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} {d} · {sig['name']} · {sig['ltf']} · {sig['label']}",
             f"{sig['session']}{' · ' + sig_clock(sig) if sig_clock(sig) else ''} · Entrée {entry_txt(sig, c)} · SL {c(sig['sl'])} ({risk:.{sig['dec']}f})"
             + (f" · signal {age_txt(sig)}" if age_txt(sig) else ""), "",
             _tp_line(sig, c), "", f"Setup : {sig['short']}"] + sig.get("trend_lines", [])
    if sig.get("inval") is not None and T.STRUCT_EXIT:
        lines.append(f"Invalidation {c(sig['inval'])}")
    tail = " · ".join(x for x in (f"Réf. #{tid}" if tid else None,) if x)
    return "\n".join(lines + ([""] + [tail] if tail else []))


def gold_message(sig, tid=None):
    """Texte de l'alerte dans le format choisi (variable ALERT_FORMAT : complet, compact, minimal, contexte, essentiel)."""
    return {"complet": message_complet, "compact": message_compact, "minimal": message_minimal,
            "contexte": message_contexte, "essentiel": message_essentiel}[alert_format()](sig, tid)


def trade_event_message(t, e, now=None):
    c = lambda v: code(v, t["dec"])
    clock = alert_clock(now or time.time())
    head = (f"{t['name']} · {t['ltf']} · {t['cascade']}" + (f" #{t['id']}" if t.get("id") else "")
            + (f" · {clock}" if clock else ""))
    closed = t["status"] == "closed" and t.get("r") is not None
    res = f"\nRésultat  {t['r']:+.1f} R" + (" (spread inclus)" if t.get("cost_r") is not None else "") if closed else ""
    if e == "filled":
        return f"▶️ Ordre limite touché · {t['otype']} · {head}\nEntrée  {c(t['entry'])}  (SL {c(t['sl'])})"
    if e == "cancel_tp":
        return f"🚫 Ordre annulé · {head}\nLe prix a atteint TP1 sans toucher l'entrée : annule l'ordre limite."
    if e == "cancel_sl":
        return (f"🚫 Ordre annulé · {head}\nUne bougie a clôturé au-delà du stop avant l'entrée : annule l'ordre limite.")
    if e == "cancel_weekend":
        return f"🚫 Ordre annulé avant le week-end · {head}\nLe marché ferme bientôt : annule l'ordre limite."
    if e == "expired":
        return f"⌛ Ordre expiré · {head}\nNon touché dans le délai prévu : annule l'ordre limite."
    if e == "tp1":
        sl_txt = f"SL à l'entrée {c(t['entry'])}" if T.BE_AT_TP1 else "SL inchangé"
        return (f"🎯 TP1 atteint · {head}\nClôture de {int(T.PARTIAL_AT_TP1 * 100)} %, {sl_txt}\n"
                f"TP1  {c(t['tp1'])}")
    if e == "tp2":
        if closed:
            return f"✅ TP2 atteint · {head}{res}"
        lock = f"SL remonté à {c(t['sl_now'])}" if T.LOCK_AT_TP2 else "SL inchangé"
        return (f"🎯 TP2 atteint · {head}\nClôture de {int(T.PARTIAL_AT_TP2 * 100)} %, {lock}, le reste vise TP3\n"
                f"TP2  {c(t['tp2'])}")
    if e == "tp3":
        return f"🏁 TP3 atteint · {head}{res}"
    if e == "sl":
        return f"❌ Stop touché · {head}{res}"
    if e == "be":
        return f"➖ Stop à l'entrée touché après TP1 · {head}{res}"
    if e == "lock":
        return f"🔒 Stop remonté touché après TP2 · {head}{res}"
    if e == "struct":
        return (f"🔻 Structure cassée · {head}\nUne bougie a clôturé au-delà de {c(t['inval'])}. "
                f"Ferme la position.{res}")
    if e == "weekend":
        return (f"🕓 Fermeture avant le week-end · {head}\nLe marché ferme bientôt : ferme la position "
                f"(les gaps d'ouverture ne sont pas maîtrisables).{res}")
    if e == "eod":
        return (f"🕓 Fin de séance · {head}\nTrade intraday : ferme la position avant la clôture.{res}")
    if e == "time":
        return (f"⏱ Sortie au temps · {head}\nTP1 non atteint en {T.TIME_STOP_BARS} bougies. "
                f"Ferme la position.{res}")
    return f"⚠️ ANNULER · {head}"


# --------------------------------------------------------------------------
# Suivi des trades, alertes, commandes Telegram
# --------------------------------------------------------------------------
def track_trades(api, tr, now, g=None):
    """Met à jour les trades : TP1 à TP3, stop, sorties de structure, sortie au temps."""
    for key, t in list(tr["active"].items()):
        if t["bot"] != BOT:
            continue
        since = max(t["seen"], t["t0"] // t["gran"] * t["gran"])
        need = min(5000, max(10, int((now - since) / t["gran"]) + 3))
        try:
            cs = api.candles(t["sym"], t["gran"], count=need)
        except Exception as ex:
            print(f"Suivi {t['name']} ignoré : {str(ex)[:80]}")
            continue
        if not cs:
            continue
        events = T.advance(t, cs, now)
        if t["status"] == "pending" and G.weekend_exit_due(t["sym"], now):
            events.append(T.cancel_pending(t, "weekend", now))
        if t["status"] == "open" and G.weekend_exit_due(t["sym"], now) and T.force_close(t, cs[-1]["close"], "weekend", now):
            events.append("weekend")
        if t["status"] == "closed":
            apply_cost(t)
        if g is not None:
            lim = g.setdefault("limits", {"placed": 0, "filled": 0, "cancelled": 0, "expired": 0})
            lim["filled"] += events.count("filled")
            lim["cancelled"] += sum(e in ("cancel_tp", "cancel_sl", "cancel_weekend") for e in events)
            lim["expired"] += events.count("expired")
        for e in events:
            notify(trade_event_message(t, e, now), html=True)
        if t["status"] not in T.ACTIVE:
            T.archive(tr, key)


def build_chart(sig, ltf_candles):
    lines = [{"y": sig["entry"], "label": "Entrée", "kind": "entry"}, {"y": sig["sl"], "label": "SL", "kind": "sl"}]
    lines += [{"y": sig[k], "label": k.upper(), "kind": "tp"} for k in ("tp1", "tp2", "tp3") if sig.get(k) is not None]
    return {"title": f"{sig['name']} · {sig['dir']} · {sig['ltf']} · {sig['label']}", "dec": sig["dec"],
            "candles": charts.candles_slice(ltf_candles), "lines": lines + sig["lines"], "zones": sig["zones"]}


def confluence_with(sig, tr, batch, now):
    """[(code, libellé, âge en minutes)] des AUTRES stratégies d'accord avec ce signal (même actif, même sens) :
    trades en attente ou ouverts alertés depuis moins de CONFLUENCE_MIN minutes, et signaux trouvés au même scan
    (âge 0). Un trade clôturé, annulé ou expiré ne compte pas. Simple information : ne filtre rien."""
    win = CONFLUENCE_MIN * 60
    if win <= 0:
        return []
    own, found = sig.get("strat"), {}
    for t in T.active_list(tr):
        code = LABEL_CODE.get(t.get("cascade")) or t.get("strat")
        age = now - t["t0"]
        if t["sym"] == sig["sym"] and t["dir"] == sig["dir"] and code and code != own and 0 <= age <= win:
            found[code] = min(age / 60, found.get(code, age / 60))
    for o in batch:
        if o is not sig and o.get("sym") == sig["sym"] and o["dir"] == sig["dir"] and o.get("strat") not in (None, own):
            found[o["strat"]] = 0.0
    return sorted((c, S.STRAT_LABELS.get(c, c), a) for c, a in found.items())


def add_confluence(sig, tr, batch, now):
    """Ajoute l'étiquette de confluence au signal (ligne de détail, résumé, colonne « confluence » du backtest)."""
    others = confluence_with(sig, tr, batch, now)
    codes = [c for c, _, _ in others]
    sig["confluence"] = codes
    sig["extra"] = dict(sig["extra"], confluence="+".join(sorted([sig["strat"]] + codes)) if codes else "aucune")
    if others:
        when = lambda a: "au même scan" if a < 1 else f"il y a {a:.0f} min"
        sig["details"] = list(sig["details"]) + [f"⚡ Confluence : {c} {lab} ({when(a)})" for c, lab, a in others]
        sig["short"] = f"{sig['short']} · confluence {'+'.join(codes)}"
    return sig


def reversal_alert(sym, provide, now, state):
    """Texte d'une alerte d'information « retournement possible » (voir setups.py), ou None. Un message par sweep ; ni ordre,
    ni suivi, ni contrôle de risque. """
    if not SU.ENABLED or "S3" in ENABLED:   # S3 envoie le même retournement en ordre
        return None
    rev = SU.detect(provide, now)
    if not rev:
        return None
    key = f"REV|{sym}|{rev['dir']}|{rev['epoch']}"
    if key in state:
        return None
    state[key] = int(now)
    return SU.message(rev, name_of(sym), dec_of(sym), alert_clock(now))


def emit_alert(sig, ltf_candles, tr, g, sent, funnel, now):
    """Filtres horaires, contrôles de risque, envoi (graphique + boutons Pris / Ignoré), enregistrement."""
    sent[sig["key"]] = int(now)   # un setup bloqué ou alerté n'est jamais réexaminé
    ok, why = G.risk_check(tr, sig["sym"], sig["dir"], now, sig["label"])
    if not ok:
        if why == "dedupe":
            funnel["dedupe"] += 1
        else:
            funnel["risk"] += 1
            print(f"Alerte bloquée {sig['label']} {sig['dir']} : {why}")
        return False
    funnel["alert"] += 1
    if is_limit(sig):
        g.setdefault("limits", {"placed": 0, "filled": 0, "cancelled": 0, "expired": 0})["placed"] += 1
    tid = g["next_id"] = g.get("next_id", 0) + 1
    text = gold_message(sig, tid)
    buttons = [[{"text": "✅ Pris", "callback_data": f"take:{tid}"},
                {"text": "❌ Ignoré", "callback_data": f"skip:{tid}"}]]
    delivered = False
    if (os.getenv("CHARTS") or "1") != "0" and TG_TOKEN and TG_CHAT:
        path = f"chart_{tid}.png"
        try:
            charts.render(build_chart(sig, ltf_candles), path)
            if len(text) <= 1000:
                delivered = notify_photo(path, text, buttons=buttons) is not None
            else:   # légende trop longue : image avec titre, puis le texte complet avec les boutons
                delivered = notify_photo(path, text.split("\n")[0]) is not None
                if delivered:
                    notify(text, html=True, buttons=buttons)
        except Exception as ex:
            print(f"Graphique ignoré : {str(ex)[:100]}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    if not delivered:
        notify(text, html=True, buttons=buttons)
    extra = dict(sig["extra"], session=sig["session"], id=tid, strat=sig["strat"],
                 fill_buf=round(spread_pct(sig["sym"]) / 100 * sig["entry"] / 2, 8), eod=intraday_exit(sig["sym"], now))
    tr["active"][sig["key"]] = T.new_trade(
        BOT, sig["key"], sig["sym"], sig["name"], sig["dir"], sig["otype"], sig["entry"], sig["sl"],
        sig["tp1"], sig["tp2"], sig["tp3"], sig["ltf"], sig["gran"], sig["dec"], sig["label"], now,
        intraday_ttl_for(sig["sym"], sig["ttl"] or TRADE_TTL, now),
        extra=extra, inval=sig["inval"], bar_secs=sig["gran"])
    return True


HELP = ("Commandes :\n/pris N : le trade #N est pris\n/ignore N : le trade #N est ignoré\n"
        "/r N X : ton résultat réel du trade #N, en R (ex. /r 12 -0.8)\n/bilan : bilan des 30 derniers jours\n"
        "/tendance : récapitulatif de tendance (D1, H4, H1) à la demande")


def _set_taken(tr, tid, taken):
    t = T.find_trade(tr, tid)
    if t is None:
        return None
    t["taken"] = taken
    return t


def handle_update(u, g, tr, now):
    cq, msg = u.get("callback_query"), u.get("message")
    chat = str((cq or {}).get("message", {}).get("chat", {}).get("id") if cq else (msg or {}).get("chat", {}).get("id"))
    if chat != str(TG_CHAT):
        return  # on n'écoute que ton chat
    if cq:
        action, _, num = (cq.get("data") or "").partition(":")
        if action in ("take", "skip") and num.isdigit():
            t = _set_taken(tr, int(num), action == "take")
            label = "✅ Pris · noté" if action == "take" else "❌ Ignoré · noté"
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"],
                                           "text": label if t else "Trade introuvable (trop ancien ?)"})
            if t:
                tg_api("editMessageReplyMarkup", {
                    "chat_id": TG_CHAT, "message_id": cq["message"]["message_id"],
                    "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}})
        else:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    parts = (msg.get("text") or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/pris", "/ignore") and args and args[0].lstrip("#").isdigit():
        t = _set_taken(tr, int(args[0].lstrip("#")), cmd == "/pris")
        notify(f"Trade #{args[0].lstrip('#')} noté « {'pris' if cmd == '/pris' else 'ignoré'} »." if t
               else "Trade introuvable (trop ancien ?).")
    elif cmd == "/r" and len(args) == 2 and args[0].lstrip("#").isdigit():
        try:
            val = float(args[1].replace(",", "."))
        except ValueError:
            notify("Format : /r N X, par exemple /r 12 -0.8")
            return
        t = _set_taken(tr, int(args[0].lstrip("#")), True)
        if t:
            t["own_r"] = val
            notify(f"Trade #{args[0].lstrip('#')} : ton résultat réel {val:+.2f} R est enregistré.")
        else:
            notify("Trade introuvable (trop ancien ?).")
    elif cmd == "/bilan":
        notify("\n".join(x for x in (bilan_text(tr, now), limits_text(g)) if x))
    elif cmd == "/tendance":
        g["recap_request"] = True      # envoyé pendant ce scan
    else:
        notify(HELP)


def bilan_text(tr, now, days=30):
    """Bilan : par stratégie (tracker), par actif, et réel vs simulé par stratégie pour les trades renseignés (/r)."""
    lines = [T.stats_text(tr, now, days=days, groups=("cascade",))]
    ts = [t for t in tr["closed"] if t.get("t_close", 0) >= now - days * 86400 and t.get("r") is not None]
    if ts:
        lines.append("Par actif :")
        for name in sorted({t["name"] for t in ts}):
            lines.append(T.fmt_summary(f"· {name}", T.summarize([t for t in ts if t["name"] == name])))
        own = [t for t in ts if t.get("own_r") is not None]
        if own:
            lines.append("Réel vs simulé (trades renseignés avec /r) :")
            for c in sorted({t["cascade"] for t in own}):
                sub = [t for t in own if t["cascade"] == c]
                real, sim = sum(t["own_r"] for t in sub), sum(t["r"] for t in sub)
                lines.append(f"· {c} : {len(sub)} trades · réel {real:+.1f} R · simulé {sim:+.1f} R (écart {real - sim:+.1f} R)")
    return "\n".join(lines)


def limits_text(g):
    """Ordres limites depuis le début : placés, touchés, annulés, expirés (taux de remplissage)."""
    lim = (g or {}).get("limits")
    if not lim or not lim.get("placed"):
        return None
    done = lim["filled"] + lim["cancelled"] + lim["expired"]
    pending = max(0, lim["placed"] - done)
    return (f"Ordres limites : {lim['placed']} placés · {lim['filled']} touchés ({100 * lim['filled'] / lim['placed']:.0f} %) · "
            f"{lim['cancelled']} annulés (TP1 ou stop avant l'entrée, week-end) · {lim['expired']} expirés · {pending} en attente")


def poll_telegram(g, tr, now):
    """Lit les boutons et commandes envoyés depuis le dernier scan (réponse sous 5 minutes)."""
    if not (TG_TOKEN and TG_CHAT):
        return
    r = tg_api("getUpdates", {"offset": g.get("tg_offset", 0), "timeout": 0,
                              "allowed_updates": ["callback_query", "message"]})
    if not r or not r.get("ok"):
        return
    for u in r.get("result", []):
        g["tg_offset"] = u["update_id"] + 1
        try:
            handle_update(u, g, tr, now)
        except Exception as ex:
            print(f"Message Telegram ignoré : {str(ex)[:100]}")


# --------------------------------------------------------------------------
# Récapitulatif de tendance (chaque matin du lundi au vendredi, une heure avant l'ouverture de Londres)
# --------------------------------------------------------------------------
RECAP_WINDOW = 6 * 3600     # envoi possible jusqu'à 6 h après l'heure prévue (retard du cron) ; au-delà, on saute la journée
RECAP_STALE = 4 * 3600      # actif sans données récentes (marché fermé) : absent du récapitulatif


def recap_config():
    """(fuseau, heure, minute) d'envoi : RECAP_FUSEAU (défaut America/New_York) et RECAP_HEURE (défaut 01:00 = une heure
    avant la killzone de Londres, qui ouvre à 02h00 New York, soit environ 07h00 à Paris). Valeurs invalides : défauts."""
    try:
        tz = ZoneInfo(os.getenv("RECAP_FUSEAU") or "America/New_York")
    except Exception:
        tz = NY
    try:
        h, m = (int(x) for x in (os.getenv("RECAP_HEURE") or "01:00").split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        h, m = 1, 0
    return tz, h, m


def recap_slot(g, now):
    """Identifiant (date locale) du créneau d'envoi en cours si le récapitulatif est dû et pas encore envoyé, sinon None.
    Du lundi au vendredi seulement (la commande /tendance fonctionne tous les jours)."""
    tz, h, m = recap_config()
    dt = datetime.fromtimestamp(now, tz)
    for back in (0, 1):                                    # créneau d'aujourd'hui, ou d'hier s'il déborde minuit
        target = (dt - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        key = target.strftime("%Y-%m-%d")
        if target.weekday() < 5 and target <= dt < target + timedelta(seconds=RECAP_WINDOW) and g.get("recap_day") != key:
            return key
    return None


def recap_due(g, now):
    return recap_slot(g, now) is not None


RECAP_FORMAT = "complet" if (os.getenv("RECAP_FORMAT") or "").strip().lower() in ("complet", "full", "long") else "compact"
# compact (défaut) : une ligne par actif, en-tête et légende courts ; complet : trois lignes par actif avec durée et invalidation.
RECAP_LEGEND_COMPACT = "Âge : naissante 1 jambe · établie 2-3 · mûre 4+ · ⚡ cassure impulsive"
RECAP_CLASSES = (("Or", [GOLD]),)       # un seul message : l'or


def build_recap(api, cache, now, syms=None):
    """Texte du récapitulatif (une liste de blocs, un par actif) ; actifs fermés ou en erreur ignorés.
    syms : actifs à traiter (défaut : tous les actifs suivis)."""
    blocks, closed = [], []
    for sym in (SYMBOLS if syms is None else syms):
        try:
            h1 = tf_candles(api, cache, sym, "H1")
            if not h1 or now - h1[-1]["epoch"] > RECAP_STALE:
                closed.append(name_of(sym))
                continue
            S.set_symbol(sym)
            infos = {tf: SU.trend_info(tf_candles(api, cache, sym, tf) or [], tf) for tf in SU.TIMEFRAMES}
            blocks.append((SU.symbol_line if RECAP_FORMAT == "compact" else SU.symbol_block)(name_of(sym), infos, now, dec_of(sym)))
        except Exception as ex:
            print(f"Récap {name_of(sym)} ignoré : {str(ex)[:80]}")
    return blocks, closed


def send_recap(api, cache, now):
    """Envoie le récapitulatif du matin : un message pour l'or.
    Les actifs fermés sont listés à la fin. Retourne False si aucun actif n'a de données."""
    day = datetime.fromtimestamp(now, NY).strftime("%d/%m")
    clock = alert_clock(now)
    compact = RECAP_FORMAT == "compact"
    legend = RECAP_LEGEND_COMPACT if compact else ("Âge = jambes consécutives (sommet + creux plus hauts, ou plus bas) : naissante 1 · établie 2-3 · mûre 4 et plus. "
                                                  "⚡ = cassure impulsive.")
    sent, closed_all = False, []
    for title, syms in RECAP_CLASSES:
        blocks, closed = build_recap(api, cache, now, [s for s in syms if s in SYMBOLS])
        if not blocks:
            closed_all += closed
            continue
        head = (f"📅 {title} · tendance · {day}" + (f" · {clock}" if clock else "")) if compact else \
            (f"📅 {title} · récapitulatif de tendance · {day}" + (f" · {clock}" if clock else "") + " (avant l'ouverture de Londres)")
        chunk = head + ("" if sent else "\n" + legend)
        sent = True
        sep = "\n" if compact else "\n\n"                 # une ligne par actif en compact, un bloc de cinq lignes sinon
        for b in blocks:
            if len(chunk) + len(b) + len(sep) > 3800:
                notify(chunk)
                chunk = ""
            chunk += (sep if chunk else "") + b
        if closed:
            chunk += f"{sep}Marché fermé : {', '.join(closed)}"
        notify(chunk)
    if sent and closed_all:
        notify(f"Marché fermé : {', '.join(closed_all)}")
    return sent


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------
def check_symbols(api):
    """Journalise les symboles absents chez Deriv (diagnostic uniquement)."""
    try:
        msg = api.request({"active_symbols": "brief"})
        names = {x.get("symbol") or x.get("underlying_symbol") for x in (msg or {}).get("active_symbols", [])}
        if names:
            missing = [x for x in SYMBOLS if x not in names]
            print(f"Symboles Deriv : {len(names)} dispo, absents de ma liste : {missing or 'aucun'}")
    except Exception as e:
        print(f"check_symbols ignoré: {e}")


_LOGGED_BLOCKS = set()      # setups bloqués déjà signalés dans le journal (le refus est réexaminé à chaque scan)


def scan(sym, provide, now, sent, funnel):
    """Évalue les stratégies actives sur un actif. Retourne [(signal, bougies de l'unité d'entrée)] non encore envoyés.
    Utilisé par le bot en direct et par le backtest."""
    S.set_symbol(sym)                  # détermine l'unité de prix de l'actif (aucun indicateur)
    m5 = provide("M5")
    if not m5 or now - m5[-1]["epoch"] > STALE_DATA:
        return []                      # données absentes ou marché fermé
    funnel["data"] += 1
    out = []
    dec = dec_of(sym)
    for sig in S.scan_all(provide, now, funnel, only=list(ENABLED), dec=dec, kz=True, modes=entry_modes()):
        sig.update(sym=sym, name=name_of(sym), dec=dec, key=f"{sym}|{sig['key']}")
        if sig["key"] in sent:
            continue
        sig["ts"] = now
        sig["age_min"] = max(0.0, (now - sig["trig_close"]) / 60) if sig.get("trig_close") else None
        infos = {tf: SU.trend_info(provide(tf) or [], tf) for tf in SU.ALERT_TFS}
        tl, te = SU.alert_lines(sig["dir"], infos, now)
        sig.update(trend_lines=tl, trend_short=SU.alert_short(sig["dir"], infos, now))
        sig["extra"] = dict(sig["extra"], **te)
        why = G.blackout(sym, now)
        if why:
            funnel["blackout"] += 1
            if sig["key"] not in _LOGGED_BLOCKS:         # un seul message de journal par setup bloqué
                _LOGGED_BLOCKS.add(sig["key"])
                print(f"Alerte bloquée {sig['name']} {sig['label']} {sig['dir']} : {why}")
            continue
        out.append((sig, provide(sig["ltf"])))
    return out


def unit_check_text(api, cache):
    """Contrôle affiché dans le message de test : unité de prix fixe de chaque actif rapportée à l'amplitude médiane réelle de ses bougies H1
   . Un écart d'un facteur supérieur à 2 signale une unité mal réglée (UNIT_PCT)."""
    bad, n = [], 0
    for sym in SYMBOLS:
        try:
            h1 = tf_candles(api, cache, sym, "H1")
        except Exception:
            continue
        if not h1 or len(h1) < 60:
            continue
        h = h1[:-1][-200:]
        S.set_symbol(sym)
        obs = sorted(c["high"] - c["low"] for c in h)[len(h) // 2]
        fx = S.unit(h)
        n += 1
        if obs > 0 and not 0.5 <= fx / obs <= 2.0:
            bad.append(f"{name_of(sym)} (unité {fx:.4g}, bougie H1 médiane {obs:.4g})")
    if not n:
        return ""
    return f"Unités de prix : {n - len(bad)}/{n} actifs cohérents" + (f" ; à vérifier : {', '.join(bad)}" if bad else "") + "."


def main():
    if "--stats" in sys.argv:
        tr, g = T.load_trades(), G.load_guard()
        print(bilan_text(tr, time.time()))
        print(limits_text(g) or "Aucun ordre limite placé.")
        for bot, counts in sorted(g["funnel"].items()):
            print(f"Entonnoir {bot} : {G.funnel_text(counts)}")
        return
    now = time.time()
    cache, state = load(CACHE_FILE), load(STATE_FILE)
    tr, g = T.load_trades(), G.load_guard()
    api, funnel = Deriv(), Counter()
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : secret Telegram manquant ou vide (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
    try:
        G.health_start(g, BOT, now, notify)
        poll_telegram(g, tr, now)
        check_symbols(api)
        track_trades(api, tr, now, g)
        manual = g.pop("recap_request", False)
        slot = recap_slot(g, now)
        if manual or slot:
            try:
                sent_ok = send_recap(api, cache, now)
                if sent_ok and slot:
                    g["recap_day"] = slot
            except Exception as ex:      # le récapitulatif ne doit jamais bloquer le scan
                print(f"Récapitulatif ignoré : {str(ex)[:100]}")
        for sym in SYMBOLS:
            memo = {}

            def provide(tf, force=False, _s=sym, _m=memo):   # un seul téléchargement par timeframe et par scan
                if tf not in _m:
                    _m[tf] = tf_candles(api, cache, _s, tf)
                return _m[tf]
            try:
                found = scan(sym, provide, now, state, funnel)
                viable = [s for s, _ in found if G.risk_check(tr, sym, s["dir"], now, s["label"])[0]]
                for sig, ltf in found:
                    add_confluence(sig, tr, viable, now)
                    emit_alert(sig, ltf, tr, g, state, funnel, now)
                text = reversal_alert(sym, provide, now, state)
                if text:
                    notify(text)
            except Exception as ex:      # un actif en échec ne doit pas bloquer les autres
                print(f"{name_of(sym)} ignoré : {str(ex)[:100]}")
        G.health_end(g, BOT, funnel["data"] > 0, now, notify)
        G.funnel_add(g, BOT, funnel)
        if not g["last_report"]:
            g["last_report"] = int(now)
        elif G.report_due(g, now):
            notify(G.build_report(g, tr, now))
            G.report_sent(g, now)
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Bot OK : {len(SYMBOLS)} actifs, {funnel['alert']} alerte(s). Stratégies actives : {', '.join(ENABLED)}.\n"
                   f"{unit_check_text(api, cache)}\n"
                   f"Entonnoir : {G.funnel_text(funnel)}\n{T.stats_text(tr, now, groups=('cascade',))}")
        print(f"Scan terminé : {len(SYMBOLS)} actifs, {funnel['alert']} alerte(s). Entonnoir : {G.funnel_text(funnel)}")
    finally:
        api.close()
        cut = time.time() - 7 * 86400
        state = {k: v for k, v in state.items() if v > cut}
        save(CACHE_FILE, cache)
        save(STATE_FILE, state)
        T.save_trades(tr)
        G.save_guard(g)


def intraday_exit(sym, now):
    """Fin de séance du trade : voir tracker.intraday_deadline."""
    return T.intraday_deadline(now)


def intraday_ttl_for(sym, ttl, now):
    """Durée de vie d'un ordre en attente : plafonnée à la fin de séance."""
    return T.intraday_ttl(ttl, now)


def ping_heartbeat():
    """Ping de surveillance : si HEARTBEAT_URL est définie (ex. healthchecks.io), on l'appelle à la fin de chaque passage
    réussi. Si les pings s'arrêtent, le service t'alerte : c'est la seule façon de savoir que le cron externe est tombé."""
    url = (os.getenv("HEARTBEAT_URL") or "").strip()
    if not url:
        return False
    try:
        requests.get(url, timeout=10)
        return True
    except Exception as e:                      # le ping ne doit jamais faire échouer le bot
        print(f"Ping de surveillance impossible : {e}")
        return False


if __name__ == "__main__":
    main()
    ping_heartbeat()
