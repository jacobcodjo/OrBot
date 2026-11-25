"""Setups de la tendance, de l'alerte de retournement, de S3 (CRT) et de S4 (Market Profiles). Toutes les règles sont décrites dans le README (section Stratégies).

Ce module regroupe quatre blocs, dans cet ordre : tendance (récapitulatif journalier et alerte), alerte de retournement (information seulement),
CRT (S3) et Market Profiles (S4). Il importe `strategies` (primitives SMC, réglages, constantes) ; `strategies` ne l'importe qu'à l'appel.

== TENDANCE ==
Tendance et âge de la tendance, pour le récapitulatif journalier (aucune alerte de trade n'en dépend).

Règles (bougies clôturées ; fractales de 2 bougies de chaque côté, 1 seule en D1 pour une lecture plus précoce),
tendance HAUSSIÈRE :
  1. le dernier sommet ET le dernier creux sont plus hauts que les précédents (2 swings suffisent) ;
  2. structure intacte : aucune clôture sous le dernier creux depuis sa formation ;
  3. cassure par une clôture (pas une simple mèche) du sommet précédent (BOS_BY_CLOSE) ;
  4. âge = nombre de jambes consécutives (sommet + creux plus hauts) : 1 -> naissante, 2-3 -> établie,
     4 et plus -> mûre (proche de sa fin).
Tendance BAISSIÈRE : l'inverse. Sinon : pas de tendance claire.
Informations ajoutées : durée depuis le début de la série de jambes, niveau d'invalidation (dernier creux
pour une hausse, dernier sommet pour une baisse), et cassure « impulsive » (corps >= 1,2 x le corps moyen).
Lecture de tendance par jambes consécutives.


== RETOURNEMENT ==
Alerte de retournement possible (information seulement : aucun ordre, aucun suivi, aucun filtre sur les stratégies).

Une tendance H4 mûre peut se retourner. On n'alerte que lorsque plusieurs éléments se cumulent :

  1. tendance H4 mûre (4 jambes consécutives ou plus) encore intacte : on prévient avant que le H4 ne casse ;
  2. sweep : une bougie H1 prend un sommet (creux) récent puis referme de l'autre côté ;
  3. ce sommet (creux) se trouve sur un niveau majeur : sommet / creux H4, zone H4, plus haut / bas de la veille ;
  4. cassure de structure H1 contre la tendance : clôture sous le dernier creux (au-dessus du dernier sommet) formé avant le sweep ;
  5. signes d'essoufflement (au moins REVERSAL_MIN_SIGNS, défaut 1 sur 2) : bougies qui rétrécissent, mèche de rejet.

Le raisonnement est écrit pour une tendance haussière ; une tendance baissière est traitée en miroir (prix inversés).


== CRT (S3) ==
S3 CRT (Candle Range Theory) pur : le range d'une bougie de référence (C1) est balayé par la bougie suivante (C2), qui clôture à
l'intérieur de ce range ; on entre en M5 (ou M15, variable S3_CONFIRM_TF=M15) sur la cassure de structure qui suit la clôture de C2, vers le milieu puis le bord opposé de C1.

  C1  bougie de référence D1 (range daily) ou H4 (la dernière clôturée avant C2) : son range est la jambe de livraison.
  C2  bougie clôturée qui dépasse le plus haut (vente) ou le plus bas (achat) de C1 et clôture DANS le range de C1.
      Une bougie qui dépasse les deux côtés (englobante) ou qui clôture hors du range n'est pas un CRT.
  C3  la bougie en formation : la livraison se fait pendant C3, donc la confirmation (M15 ou M5) doit arriver avant sa clôture.
  Entrée : première clôture M15 (ou M5) au-delà du dernier swing de ce timeframe, après la clôture de C2 (CHoCH avec déplacement), ordre limite sur le
          bloc d'ordre ou le FVG de ce timeframe s'il y en a un. Stop : au-delà de l'extrême de C2. Objectifs : milieu de C1, bord opposé de C1,
          bord opposé + 50 % du range ; ratio minimal 1 pour 3 calculé sur TP1 (milieu de C1), comme les autres stratégies.

Les réglages (CRT_*, variables S3_*) sont dans strategies.py. strategies importe ce module en fin de fichier : on n'importe donc strategies qu'à
l'appel, pas au chargement.

"""
import os
from datetime import datetime

import strategies as S
from strategies import is_displacement, close_beyond


# ==========================================================================
# TENDANCE
# ==========================================================================

SWING_N = 2
SWING_N_BY_TF = {"D1": 1}   # D1 : un swing est confirmé 1 jour après (et non 2) ; réglable, à comparer au backtest
AGE_ESTABLISHED = 2       # jambes consécutives : 1 = naissante
AGE_MATURE = 4            # 4 jambes ou plus = mûre
BOS_BY_CLOSE = True
MIN_CANDLES = 12
TIMEFRAMES = ("D1", "H4", "H1")


def _run(vals):
    """Nombre de hausses successives à la fin de la liste (ex : 1,2,3 -> 2)."""
    n = 0
    for a, b in zip(vals[-2::-1], vals[:0:-1]):
        if b > a:
            n += 1
        else:
            break
    return n


def flip(cs):
    return [{"epoch": c["epoch"], "open": -c["open"], "high": -c["low"], "low": -c["high"], "close": -c["close"]}
            for c in cs]


def _up(h, n=SWING_N):
    """Tendance haussière sur des bougies clôturées (fractales de n bougies), ou None."""
    if len(h) < MIN_CANDLES:
        return None
    sh, sl = S.swings(h, "high", n), S.swings(h, "low", n)
    if len(sh) < 2 or len(sl) < 2:
        return None
    highs, lows = [h[i]["high"] for i in sh], [h[i]["low"] for i in sl]
    if not (highs[-1] > highs[-2] and lows[-1] > lows[-2]):
        return None
    if any(c["close"] < lows[-1] for c in h[sl[-1] + 1:]):
        return None                                   # structure cassée
    brk = next((i for i in range(sh[-2] + 1, len(h)) if h[i]["close"] > highs[-2]), None)
    if BOS_BY_CLOSE and brk is None:
        return None                                   # sommet dépassé seulement par une mèche
    legs = max(1, min(_run(highs), _run(lows)))
    age = "naissante" if legs < AGE_ESTABLISHED else "établie" if legs < AGE_MATURE else "mûre"
    start = min(sh[-(legs + 1)], sl[-(legs + 1)])
    impulsive = False
    if brk is not None and brk >= 1:
        avg = S.ref_body(h)
        impulsive = avg > 0 and S.body(h[brk]) >= S.DISP_MULT * avg
    return {"legs": legs, "age": age, "start_epoch": h[start]["epoch"], "invalid": lows[-1],
            "impulsive": impulsive}


def trend_info(candles, tf=None):
    """candles : bougies du timeframe, la dernière est en formation (ignorée).
    Retourne {"dir": "BUY"|"SELL", "age", "legs", "start_epoch", "invalid", "impulsive"} ou None."""
    h = candles[:-1]
    n = SWING_N_BY_TF.get(tf, SWING_N)
    up = _up(h, n)
    if up:
        return dict(up, dir="BUY")
    dn = _up(flip(h), n)
    if dn:
        return dict(dn, dir="SELL", invalid=-dn["invalid"])
    return None


def fmt_duration(sec):
    sec = max(0, sec)
    return f"{sec / 86400:.0f} j" if sec >= 2 * 86400 else f"{max(1, round(sec / 3600))} h"


def tf_line(tf, info, now, dec):
    if info is None:
        return f"  {tf}  ⚪ Pas de tendance claire"
    buy = info["dir"] == "BUY"
    legs = f"{info['legs']} jambe" + ("s" if info["legs"] > 1 else "")
    return (f"  {tf}  {'🟢 Haussière' if buy else '🔴 Baissière'} · {info['age']} ({legs}, "
            f"{fmt_duration(now - info['start_epoch'])}){' ⚡' if info['impulsive'] else ''} · "
            f"invalidation {info['invalid']:.{dec}f}")


def summary(infos):
    """Phrase de synthèse sur l'alignement D1 / H4 / H1. infos : {tf: info ou None}."""
    dirs = {tf: (infos.get(tf) or {}).get("dir") for tf in TIMEFRAMES}
    word = {"BUY": "haussiers", "SELL": "baissiers"}
    if dirs["D1"] and dirs["D1"] == dirs["H4"] == dirs["H1"]:
        return f"→ D1, H4 et H1 alignés ({word[dirs['D1']]})"
    if dirs["D1"] and dirs["D1"] == dirs["H4"]:
        rest = "sans tendance claire" if not dirs["H1"] else f"à contre-sens ({word[dirs['H1']]})"
        return f"→ D1 et H4 alignés ({word[dirs['D1']]}), H1 {rest}"
    if dirs["H4"] and dirs["H4"] == dirs["H1"]:
        rest = "sans tendance claire" if not dirs["D1"] else f"à contre-sens ({word[dirs['D1']]})"
        return f"→ H4 et H1 alignés ({word[dirs['H4']]}), D1 {rest}"
    return "→ timeframes non alignés"


ALERT_TFS = ("D1", "H4")


def alert_lines(d, infos, now):
    """Lignes de tendance (D1 et H4) ajoutées à une alerte de trade de sens d ('BUY'/'SELL'), et champs
    d'enregistrement pour le backtest. ✅ = tendance dans le sens du trade, ⚠️ = à contre-sens.
    C'est une information : aucune alerte n'est filtrée sur la tendance ou son âge."""
    lines, extra = [], {}
    for tf in ALERT_TFS:
        info = infos.get(tf)
        if info is None:
            lines.append(f"⚪ Tendance {tf} : pas de tendance claire")
            extra[f"tend_{tf.lower()}"] = "aucune"
            continue
        ok = info["dir"] == d
        legs = f"{info['legs']} jambe" + ("s" if info["legs"] > 1 else "")
        lines.append(f"{'✅' if ok else '⚠️'} Tendance {tf} {'haussière' if info['dir'] == 'BUY' else 'baissière'}"
                     f" · {info['age']} ({legs}, {fmt_duration(now - info['start_epoch'])})"
                     f"{'' if ok else ' : contre le trade'}")
        extra[f"tend_{tf.lower()}"] = f"{info['age']} {'avec' if ok else 'contre'}"
    return lines, extra


def alert_short(d, infos, now):
    """Tendance D1/H4 en une ligne, ex. « ✅ D1 haussière établie (9 j) · ⚠️ H4 baissière mûre (6 j) »."""
    parts = []
    for tf in ALERT_TFS:
        info = infos.get(tf)
        if info is None:
            parts.append(f"⚪ {tf} sans tendance")
            continue
        parts.append(f"{'✅' if info['dir'] == d else '⚠️'} {tf} {'haussière' if info['dir'] == 'BUY' else 'baissière'} "
                     f"{info['age']} ({fmt_duration(now - info['start_epoch'])})")
    return " · ".join(parts)


def symbol_line(name, infos, now, dec):
    """Récapitulatif compact : une ligne par actif, ex. « XAUUSD  🟢 D1 établie ⚡ · 🟢 H4 mûre · ⚪ H1 ». La couleur donne le sens,
    donc l'alignement des timeframes se lit d'un coup d'œil ; l'invalidation et la durée sont dans le format complet."""
    parts = []
    for tf in TIMEFRAMES:
        info = infos.get(tf)
        if info is None:
            parts.append(f"⚪ {tf}")
        else:
            parts.append(f"{'🟢' if info['dir'] == 'BUY' else '🔴'} {tf} {info['age']}{' ⚡' if info['impulsive'] else ''}")
    return f"{name}  " + " · ".join(parts)


def symbol_block(name, infos, now, dec):
    return "\n".join([name] + [tf_line(tf, infos.get(tf), now, dec) for tf in TIMEFRAMES] + [f"  {summary(infos)}"])

# ==========================================================================
# RETOURNEMENT
# ==========================================================================

ENABLED = (os.getenv("REVERSAL_ALERTS") or "1").strip() != "0"      # REVERSAL_ALERTS=0 : désactive l'alerte
AGES = ("mûre",)                  # âges de tendance H4 qui peuvent s'essouffler
REQUIRE_LEVEL = True              # le sommet (creux) pris doit être sur un niveau majeur H4 / veille
SWEEP_BARS = 6                    # le sweep date de moins de 6 bougies H1
FRESH_BARS = 2                    # la cassure de structure date de 2 bougies H1 au plus
PIVOT_BARS = 12                   # le creux (sommet) cassé a été formé dans les 12 bougies avant le sweep
LEVEL_BARS = 80                   # sommets (creux) candidats : les 80 dernières bougies H1
REV_SWING_N = 2
STALE = 3 * 3600                  # données H1 plus vieilles : marché fermé, pas d'alerte
FADE_RATIO = 0.6                  # corps moyen des 3 dernières bougies < 60 % du corps de référence (0,4 unité)
WICK_RATIO = 2.0                  # mèche haute >= 2 fois le corps et >= la moitié de la bougie
MAJOR_WORDS = ("H4", "veille")    # niveaux « majeurs » parmi les obstacles de strategies.htf_obstacles


def _body(c):
    return abs(c["close"] - c["open"])


def shrinking_candles(h, e):
    """Élan qui s'éteint : corps moyen des 3 bougies jusqu'à e (comprise) < FADE_RATIO fois le corps de référence du timeframe
    (0,4 unité de prix, fixe : aucune moyenne glissante du marché). Il faut au moins 12 bougies d'historique."""
    if e < 12:
        return False
    last = [_body(c) for c in h[e - 2:e + 1]]
    ref = S.ref_body(h[:e + 1])
    return ref > 0 and sum(last) / len(last) < FADE_RATIO * ref


def rejection_wick(c):
    """Longue mèche haute : >= WICK_RATIO fois le corps et >= la moitié de l'amplitude de la bougie."""
    rng = c["high"] - c["low"]
    upper = c["high"] - max(c["open"], c["close"])
    return rng > 0 and upper >= WICK_RATIO * max(_body(c), 1e-12) and upper >= 0.5 * rng


def find_reversal(h):
    """Sweep + cassure de structure sur des bougies H1 clôturées, écrits pour une tendance haussière.
    Retourne {'level_i', 'sweep_i', 'ext_i', 'pivot_i', 'trig_i'} ou None."""
    n = len(h)
    if n < 40:
        return None
    highs = [i for i in S.swings(h, "high", REV_SWING_N) if i >= n - LEVEL_BARS]
    lows = S.swings(h, "low", REV_SWING_N)
    for i in range(n - 1, max(n - 1 - SWEEP_BARS, REV_SWING_N), -1):          # du sweep le plus récent au plus ancien
        c = h[i]
        for p in sorted((x for x in highs if x <= i - REV_SWING_N - 1), reverse=True):
            lv = h[p]["high"]
            if not (c["high"] > lv > c["close"]):
                continue
            if any(h[j]["high"] > lv for j in range(p + 1, i)):            # sommet déjà pris : plus de liquidité
                continue
            e = max(range(i, n), key=lambda j: (h[j]["high"], -j))          # extrême après le sweep
            cand = [q for q in lows if e - PIVOT_BARS <= q < e]
            if not cand:
                continue
            q = max(cand)
            pivot = h[q]["low"]
            trig = next((k for k in range(e + 1, n) if h[k]["close"] < pivot), None)
            if trig is None or trig < n - FRESH_BARS:
                continue
            return {"level_i": p, "sweep_i": i, "ext_i": e, "pivot_i": q, "trig_i": trig}
    return None


def detect(provide, now):
    """Retournement possible sur un actif, ou None. provide(tf) -> bougies (la dernière est en formation).
    Retourne un dict : dir (sens du retournement), trend_dir, age, legs, level (px, nom), ext, pivot, signs, key, epoch."""
    h4, h1 = provide("H4"), provide("H1")
    if not h4 or not h1 or len(h1) < 41 or now - h1[-2]["epoch"] > STALE:
        return None
    t4 = trend_info(h4, "H4")
    if not t4 or t4["age"] not in AGES:
        return None
    up = t4["dir"] == "BUY"
    h = h1[:-1] if up else S.mirror(h1[:-1])              # miroir pour une tendance baissière
    found = find_reversal(h)
    if not found:
        return None
    mir = (lambda x: x) if up else (lambda x: -x)             # prix du miroir -> prix réels
    lv_px, ext_px, pivot_px = mir(h[found["level_i"]]["high"]), mir(h[found["ext_i"]]["high"]), mir(h[found["pivot_i"]]["low"])
    obs = S.htf_obstacles(provide, now)
    level_name = None
    if obs:
        tol = S.OBST_TOL_ATR * obs["atr"]
        pool = obs["res"] if up else obs["sup"]
        for lo, hi, nm in pool:
            if any(w in nm for w in MAJOR_WORDS) and lo - tol <= lv_px <= hi + tol:
                level_name = nm
                break
    if REQUIRE_LEVEL and level_name is None:
        return None
    signs = []
    if shrinking_candles(h, found["ext_i"]):
        signs.append("bougies qui rétrécissent")
    if rejection_wick(h[found["ext_i"]]) or rejection_wick(h[found["sweep_i"]]):
        signs.append("mèche de rejet")
    if len(signs) < MIN_SIGNS:
        return None                                   # pas assez de signes d'essoufflement
    ext_epoch = h[found["ext_i"]]["epoch"]
    d = "SELL" if up else "BUY"
    return {"dir": d, "trend_dir": t4["dir"], "age": t4["age"], "legs": t4["legs"], "level": (lv_px, level_name or "niveau"),
            "ext": ext_px, "pivot": pivot_px, "signs": signs, "epoch": ext_epoch}


def message(rev, name, dec, clock=""):
    """Texte de l'alerte (information)."""
    word = {"BUY": "haussier", "SELL": "baissier"}
    side = "au-dessus" if rev["dir"] == "SELL" else "en dessous"
    taken = "sommet" if rev["dir"] == "SELL" else "creux"
    broke = "sous le dernier creux" if rev["dir"] == "SELL" else "au-dessus du dernier sommet"
    lines = [f"🔄 Retournement possible · {name} · {word[rev['dir']]}",
             f"Contre une tendance H4 {word[rev['trend_dir']]} mûre ({rev['legs']} jambes), encore intacte",
             f"✅ Sweep : {taken} H1 {rev['level'][0]:.{dec}f} ({rev['level'][1]}) pris puis rejeté (extrême {rev['ext']:.{dec}f})",
             f"✅ Cassure de structure H1 : clôture {broke} ({rev['pivot']:.{dec}f})",
             *(["✅ Essoufflement : " + " · ".join(rev["signs"])] if rev["signs"] else []),
             f"Invalidation : retour {side} de {rev['ext']:.{dec}f}",
             "ℹ️ Information : aucun ordre, aucun suivi. Attendre la cassure du H4 pour confirmer." + (f" {clock}" if clock else "")]
    return "\n".join(lines)


# Nombre de signes d'essoufflement exigés (sur 2) pour l'alerte de retournement
try:
    MIN_SIGNS = int(os.getenv("REVERSAL_MIN_SIGNS") or 1)             # signes d'essoufflement exigés (sur 2)
except ValueError:
    MIN_SIGNS = 1


# ==========================================================================
# CRT (S3)
# ==========================================================================

def crt_pair(S, rc, funnel=None):
    """(sens, C1, C2, ATR) si les deux dernières bougies clôturées `rc` forment un CRT : C2 balaie UN côté de C1 (d'au moins
    CRT_SWEEP_MIN_ATR x ATR) et clôture dans son range, et le range de C1 a une taille raisonnable (entre CRT_MIN_RANGE_ATR et
    CRT_MAX_RANGE_ATR x ATR) : assez grand pour qu'un stop serré donne 1:3 jusqu'au milieu du range, pas démesuré. Sinon None."""
    if len(rc) < 17:
        return None
    c1, c2 = rc[-2], rc[-1]
    a = S.unit(rc)
    if a <= 0:
        return None
    up, dn = c2["high"] > c1["high"], c2["low"] < c1["low"]
    if up == dn:                                         # aucun sweep, ou bougie englobante (les deux côtés)
        return None
    if not (c1["low"] < c2["close"] < c1["high"]):       # C2 doit clôturer dans le range de C1
        return None
    pen = (c2["high"] - c1["high"]) if up else (c1["low"] - c2["low"])
    if S.CRT_SWEEP_MIN_ATR and pen < S.CRT_SWEEP_MIN_ATR * a:
        if funnel is not None:
            funnel["CRT_sweep"] += 1                     # mèche à peine au-delà du bord de C1 : pas un vrai balayage
        return None
    if not (S.CRT_MIN_RANGE_ATR * a <= c1["high"] - c1["low"] <= S.CRT_MAX_RANGE_ATR * a):
        if funnel is not None:
            funnel["CRT_taille"] += 1                    # range de C1 trop petit ou démesuré
        return None
    return ("SELL" if up else "BUY"), c1, c2, a


def crt_confirm(S, d, c2, tf, cfc, cf_n="M15"):
    """Cassure de structure (M15 ou M5, `cf_n`) après la clôture de C2, pendant C3. Retourne {'ext_i', 'ext_px', 'trig', 'pivot'} ou None.
    L'extrême de C2 ne doit pas avoir été repris depuis sa clôture (sinon le sweep a échoué). Le pivot est le dernier swing M15
    (fractale de 2 bougies) confirmé avant la bougie testée, à moins de CRT_PIVOT_BARS bougies de l'extrême de C2 ; la cassure est la
    première clôture au-delà de ce pivot, et doit être l'une des FRESH dernières bougies clôturées."""
    c2_end = c2["epoch"] + S.GRAN[tf]
    ext_px = c2["high"] if d == "SELL" else c2["low"]
    inside = [i for i, c in enumerate(cfc) if c2["epoch"] <= c["epoch"] < c2_end]
    after = [i for i, c in enumerate(cfc) if c["epoch"] >= c2_end]
    if not inside or not after:
        return None
    ext_i = (max(inside, key=lambda i: (cfc[i]["high"], i)) if d == "SELL"
             else min(inside, key=lambda i: (cfc[i]["low"], -i)))
    if any((cfc[i]["high"] > ext_px) if d == "SELL" else (cfc[i]["low"] < ext_px) for i in after):
        return None                                      # l'extrême de C2 a été repris : pas de retournement
    pk = "low" if d == "SELL" else "high"
    sw = S.swings(cfc, pk, 2)
    for j in after:
        if cfc[j]["epoch"] >= c2_end + S.GRAN[tf]:       # on est sorti de C3
            break
        piv = [i for i in sw if i >= ext_i - S.bars(S.CRT_PIVOT_BARS, cf_n) and i + 2 < j]
        if piv and close_beyond(cfc[j], cfc[piv[-1]][pk], d == "BUY"):
            if j < len(cfc) - S.FRESH:
                return None                              # cassure trop ancienne
            return {"ext_i": ext_i, "ext_px": ext_px, "trig": j, "pivot": cfc[piv[-1]][pk]}
    return None


CRT_C2_REPORT_HOURS = (1, 5, 9)        # bougies H4 (heure d'ouverture de C2, New York) suivies dans le rapport du backtest


def _crt_c2_h4(S, tf, c2):
    """Étiquette du rapport de backtest : heure d'ouverture (New York) de la bougie H4 C2, pour 01h, 05h et 09h seulement."""
    if tf != "H4":
        return {}
    h = datetime.fromtimestamp(c2["epoch"], S.NY).hour
    return {"c2_h4": f"{h:02d}h"} if h in CRT_C2_REPORT_HOURS else {}


def _crt_one(S, tf, ref, m15, m15c, a_m15, a_h1, funnel, dec, kz, mode, cf_n="M15"):
    pair = crt_pair(S, ref[:-1], funnel)
    if pair is None:
        return None
    d, c1, c2, a_ref = pair
    funnel["CRT_c2"] += 1
    cfm = crt_confirm(S, d, c2, tf, m15c, cf_n)
    if cfm is None:
        return None
    funnel["CRT_choch"] += 1
    ext_i, ext_px, trig, pivot = cfm["ext_i"], cfm["ext_px"], cfm["trig"], cfm["pivot"]
    tc = m15c[trig]
    if not is_displacement(tc, S.ref_body(m15c), S.DISP_MULT):
        return None
    sess = S.session_name(tc["epoch"])                   # affichage seulement : la cassure suit la clôture de C2, à toute heure
    lo1, hi1 = c1["low"], c1["high"]
    rng = hi1 - lo1
    mid = (hi1 + lo1) / 2
    sgn = 1 if d == "BUY" else -1
    price = m15[-1]["close"]
    sl = ext_px + S.CRT_SL_BUF_ATR * a_m15 if d == "SELL" else ext_px - S.CRT_SL_BUF_ATR * a_m15
    poi = S.entry_zone(m15c, ext_i, trig - 1, d)
    if S.S3_STOP_MODE == "ob" and poi:
        sl = S.zone_stop(d, poi[2], a_m15)               # stop à l'autre extrémité du bloc d'ordre (ou du FVG) d'entrée
    otype, entry = S.pick_entry(d, price, poi[0] if poi else None, sl, mode)
    if otype != d:
        if abs(price - entry) > S.CRT_LIMIT_MAX_ATR * a_m15:
            funnel["CRT_stale"] += 1
            return None                                  # zone d'entrée trop loin du prix
    elif S.is_late(d, price, tc["close"], a_m15, S.CRT_CHASE_ATR):
        funnel["CRT_stale"] += 1
        return None                                      # le prix a déjà fui après la cassure
    tp1 = mid
    tp2 = hi1 if d == "BUY" else lo1
    tp3 = tp2 + sgn * S.CRT_TP3_EXT * rng
    if sgn * (tp1 - entry) <= 0:                         # le milieu de C1 est déjà atteint : la livraison a eu lieu
        funnel["stale"] += 1
        return None
    lv = S.build_levels(d, entry, sl, a_m15, tps=[tp1, tp2, tp3])
    if lv is None:
        return None
    if S.CRT_MIN_RR and abs(tp1 - entry) / lv["risk"] < S.CRT_MIN_RR:
        funnel["CRT_rr"] += 1
        return None                                      # ratio gain/risque jusqu'au milieu de C1 (TP1) sous le minimum
    funnel["CRT_signal"] += 1
    side_hi = d == "SELL"
    details = [f"✅ CRT {tf} : C2 a balayé le {'plus haut' if side_hi else 'plus bas'} de C1 ({hi1 if side_hi else lo1:.{dec}f}, "
               f"mèche {ext_px:.{dec}f}) et clôturé dans son range",
               f"✅ Range C1 {lo1:.{dec}f} - {hi1:.{dec}f} ({rng / a_ref:.1f} ATR {tf})",
               f"✅ CHoCH {cf_n} {'sous' if side_hi else 'au-dessus de'} {pivot:.{dec}f} après la clôture de C2"]
    details.append(f"🎯 Objectifs sur C1 : milieu {tp1:.{dec}f}, bord opposé {tp2:.{dec}f}, bord opposé + {S.CRT_TP3_EXT * 100:.0f} % du range {tp3:.{dec}f}")
    return S._signal(
        "S3", "CRT", d, entry, lv, m15, cf_n, f"{tf}|{c2['epoch']}", details,
        lines=[{"y": hi1, "label": f"C1 haut {tf}", "kind": "range"}, {"y": lo1, "label": f"C1 bas {tf}", "kind": "range"},
               {"y": mid, "label": "Milieu C1", "kind": "info"}, {"y": ext_px, "label": "Sweep C2", "kind": "sweep"},
               {"y": pivot, "label": "CHoCH", "kind": "info"}],
        zones=[{"lo": lo1, "hi": hi1, "label": f"Range C1 {tf}", "kind": "fib"}],
        inval=ext_px, session=sess,
        extra=dict({"ref": tf, "c1": f"{lo1:.{dec}f}-{hi1:.{dec}f}", "poi": poi[1] if otype != d else "marché", "tp": "range C1"},
                   **_crt_c2_h4(S, tf, c2)),
        short=f"CRT {tf} · C2 clôturée dans C1 · CHoCH {cf_n}", trig=tc["epoch"], otype=otype, price_now=price,
        ttl=S.LIMIT_TTL["S3"] if otype != d else None,
        entry_note=S.poi_note(poi, cf_n, dec) if otype != d else "")


def scan_crt(provide, now, funnel, dec=2, kz=True, mode="market"):
    """S3 CRT : une liste de signaux (au plus un par bougie de référence : H4 et H1), vide s'il n'y a rien."""
    import strategies as S
    cf_n = S.confirm_tf("S3")                         # confirmation : M5 (défaut) ou M15 (m15 / m15c / a_m15 : bougies et ATR de ce timeframe)
    m15, h1 = provide(cf_n), provide("H1")
    if not (m15 and h1):
        return []
    m15c, h1c = m15[:-1], h1[:-1]
    if len(m15c) < 40 or len(h1c) < 40:
        return []
    a_m15, a_h1 = S.unit(m15c), S.unit(h1c)
    if min(a_m15, a_h1) <= 0:
        return []
    out = []
    for tf in S.CRT_REFS:
        ref = provide(tf)
        if not ref:
            continue
        sig = _crt_one(S, tf, ref, m15, m15c, a_m15, a_h1, funnel, dec, kz, mode, cf_n)
        if sig and _crt_gate(S, sig, provide, now, funnel):
            out.append(sig)
    return out


def _crt_gate(S, sig, provide, now, funnel):
    """Garde-fous appliqués au signal construit : âge du déclencheur, contexte H4/D1, obstacles H4/H1 (intraday)."""
    if S.CRT_MAX_AGE_MIN and sig.get("trig_close") and (now - sig["trig_close"]) / 60 > S.CRT_MAX_AGE_MIN:
        funnel["CRT_age"] += 1                           # déclencheur trop ancien (retard du cron ou du workflow)
        return False
    if S.CRT_CONTEXT_ALIGN:
        for tf in ("H4", "D1"):                          # pas contre une tendance H4 ou D1 claire
            cs = provide(tf)
            info = trend_info(cs, tf) if cs and len(cs) >= 10 else None
            if info is not None and info["dir"] != sig["dir"]:
                funnel["CRT_contexte"] += 1
                return False
    if S.INTRADAY:
        why = S.obstacle_filter(sig, provide, now, "CRT", False, S.CRT_MIN_RR, "tp1")
        if why:
            funnel[why] += 1                             # obstacle H4/H1 trop proche devant l'objectif
            return False
    return True


# ==========================================================================
# S4 Market Profiles (profiles.py : range -> manipulation -> expansion, prix pur)
# ==========================================================================
def _profile_key_levels(S, provide, now, a_h1):
    """Niveaux clés (prix) : PDH/PDL, extrêmes de la plage asiatique, equal highs/lows H1 (liquidity_levels) et derniers sommets/creux H4.
    Les niveaux à moins de 0,1 unité H1 l'un de l'autre sont fusionnés."""
    h1, m15, d1, h4 = provide("H1"), provide("M15"), provide("D1"), provide("H4")
    out = []
    if h1 and m15:
        out += [px for _, px, _, _ in S.liquidity_levels(h1[:-1], d1 or [], m15[:-1], now, a_h1)]
    if h4 and len(h4) > 12:
        h4c = h4[:-1]
        for kind in ("high", "low"):
            out += [h4c[i][kind] for i in S.swings(h4c, kind, 2)[-S.S4_H4_SWINGS:]]
    uniq = []
    for px in sorted(out):
        if not uniq or px - uniq[-1] > 0.1 * a_h1:
            uniq.append(px)
    return uniq


def _profile_tps(S, P, closed, d, entry, risk, tp1):
    """TP2 / TP3 : liquidité opposée suivante (pivot) à plus de 0,5 R de l'objectif précédent et à moins de S4_TP_MAX_R au-delà de TP1,
    sinon l'objectif précédent + 1 R. TP1 vient de profiles.py (première liquidité opposée, sinon le ratio minimal)."""
    sgn = 1 if d == "BUY" else -1
    typ = "H" if d == "BUY" else "L"
    liq = sorted((p for _, t, p in P.pivots(closed) if t == typ and 0 < sgn * (p - tp1) <= S.S4_TP_MAX_R * risk), key=lambda p: sgn * p)
    tps, prev = [], tp1
    for _ in range(2):
        nxt = next((p for p in liq if sgn * (p - prev) > 0.5 * risk), None)
        prev = nxt if nxt is not None else prev + sgn * risk
        tps.append(prev)
    return tps[0], tps[1]


def _profile_one(S, P, st, closed, cs, tf, a_ltf, a_h1, funnel, dec, kz, mode):
    d = st.side
    sgn = 1 if d == "BUY" else -1
    if not 0 <= st.trig < len(closed):
        return None
    tc = closed[st.trig]
    sess = S.session_name(tc["epoch"])
    if kz and S.S4_SESSION_FILTER and sess not in S.S4_SESSIONS:
        funnel["S4_session"] += 1
        return None                                      # marchés à séance : déclencheur hors Londres / New York / London Close
    price = cs[-1]["close"]
    retest_done = abs(price - st.entry) < 0.1 * a_ltf    # setup B après retest : l'entrée est le prix actuel
    if not retest_done and sgn * (price - st.entry) < 0:
        funnel["S4_stale"] += 1
        return None                                      # le prix est repassé du mauvais côté du niveau de retest
    sl = st.sl - sgn * S.S4_SL_BUF_ATR * a_ltf
    otype, entry = S.pick_entry(d, price, None if retest_done else st.entry, sl, mode)
    if otype != d:
        if abs(price - entry) > S.S4_LIMIT_MAX_ATR * a_ltf:
            funnel["S4_stale"] += 1
            return None                                  # niveau de retest trop loin du prix
    elif S.is_late(d, price, tc["close"], a_ltf, S.S4_CHASE_ATR):
        funnel["S4_stale"] += 1
        return None                                      # le prix a déjà fui après le déclencheur
    risk0 = abs(entry - sl)
    tp1 = st.tp1
    if risk0 <= 0 or sgn * (tp1 - entry) <= 0:
        funnel["stale"] += 1
        return None                                      # objectif déjà atteint
    min_rr = S.S4_MIN_RR or 3.0
    liq = abs(tp1 - (st.entry + sgn * min_rr * abs(st.entry - st.sl))) > 1e-9 * max(1.0, abs(tp1))   # TP1 = vraie liquidité (sinon : ratio minimal)
    lv = S.build_levels(d, entry, sl, a_ltf, tps=[tp1, tp1, tp1])
    if lv is None:
        return None
    if not liq:
        tp1 = entry + sgn * min_rr * lv["risk"]          # sans liquidité : TP1 au ratio minimal du risque réel (marge du stop comprise)
    tp2, tp3 = _profile_tps(S, P, closed, d, entry, lv["risk"], tp1)
    lv["tp"] = [tp1, tp2, tp3]
    if S.S4_MIN_RR and abs(tp1 - entry) / lv["risk"] < S.S4_MIN_RR - 1e-9:
        funnel["S4_rr"] += 1
        return None                                      # ratio gain/risque sous le minimum après la marge du stop
    if S.S4_RISK_CAP_H1 and lv["risk"] > S.S4_RISK_CAP_H1 * a_h1:
        funnel["S4_risque"] += 1
        return None                                      # stop trop large
    letter, m = st.name.split()[0], st.meta
    is_b = letter == "B"
    details = [f"✅ Setup {st.name} : {st.note}"]
    if m.get("range"):
        details.append(f"✅ Range {m['range'][0]:.{dec}f} - {m['range'][1]:.{dec}f}")
    if m.get("key") is not None:
        details.append(f"✅ Niveau clé {m['key']:.{dec}f}")
    if m.get("extreme") is not None:
        details.append(f"✅ {'Stop au-delà de l’extrême' if is_b else 'Sweep à'} {m['extreme']:.{dec}f}")
    if m.get("level") is not None:
        details.append(f"✅ Cassure du niveau {m['level']:.{dec}f}, retest attendu" if is_b else
                       f"✅ CHoCH : clôture {'au-dessus' if d == 'BUY' else 'sous'} {m['level']:.{dec}f}")
    details.append("🎯 TP1 sur la prochaine liquidité opposée" if liq else f"🎯 TP1 au ratio minimal ({S.S4_MIN_RR or 3.0:g} R) : aucune liquidité opposée repérée")
    lines = []
    if m.get("range"):
        lines += [{"y": m["range"][1], "label": "Range haut", "kind": "range"}, {"y": m["range"][0], "label": "Range bas", "kind": "range"}]
    if m.get("key") is not None:
        lines.append({"y": m["key"], "label": "Niveau clé", "kind": "info"})
    if m.get("extreme") is not None and not is_b:
        lines.append({"y": m["extreme"], "label": "Sweep", "kind": "sweep"})
    if m.get("level") is not None:
        lines.append({"y": m["level"], "label": "Retest" if is_b else "CHoCH", "kind": "info"})
    zones = [{"lo": m["range"][0], "hi": m["range"][1], "label": "Range", "kind": "fib"}] if m.get("range") else []
    return S._signal(
        "S4", "Market Profiles", d, entry, lv, cs, tf, f"{letter}|{tc['epoch']}", details,
        lines=lines, zones=zones, inval=st.sl, session=sess,
        extra={"setup": st.name, "poi": "niveau" if otype != d else "marché", "tp": "liquidité" if liq else "ratio"},
        short=f"{st.name} · {tf}", trig=tc["epoch"], otype=otype, price_now=price,
        ttl=S.LIMIT_TTL["S4"] if otype != d else None,
        entry_note=f"Ordre limite au retest de {entry:.{dec}f} ({st.note})" if otype != d else "")


def _profile_gate(S, sig, provide, now, funnel):
    """Garde-fous appliqués au signal construit : âge du déclencheur, obstacles H4/H1 (intraday)."""
    if S.S4_MAX_AGE_MIN and sig.get("trig_close") and (now - sig["trig_close"]) / 60 > S.S4_MAX_AGE_MIN:
        funnel["S4_age"] += 1                            # déclencheur trop ancien (retard du cron ou du workflow)
        return False
    if S.INTRADAY:
        why = S.obstacle_filter(sig, provide, now, "S4", False, S.S4_MIN_RR, "tp1")
        if why:
            funnel[why] += 1                             # obstacle H4/H1 trop proche devant l'objectif
            return False
    funnel["S4_signal"] += 1
    return True


def scan_profiles(provide, now, funnel, dec=2, kz=True, mode="market"):
    """S4 Market Profiles : une liste de signaux (au plus un par setup A à E), vide s'il n'y a rien.
    Les 5 setups de profiles.py (range + sweep, break & retest, reversal sur niveau clé, manipulation sur support, retracement + deep
    sweep) sont évalués sur les bougies clôturées du timeframe S4_TF ; les niveaux clés viennent du bot (veille, plage asiatique, EQH/EQL, H4)."""
    import profiles as P
    import strategies as S
    tf = S.S4_TF
    cs, h1 = provide(tf), provide("H1")
    if not (cs and h1):
        return []
    closed, h1c = cs[:-1][-S.S4_BARS:], h1[:-1]
    if len(closed) < 40 or len(h1c) < 40:
        return []
    a_ltf, a_h1 = S.unit(closed), S.unit(h1c)
    if min(a_ltf, a_h1) <= 0:
        return []
    levels = _profile_key_levels(S, provide, now, a_h1)
    out = []
    for st in P.scan_all(closed, levels, min_rr=S.S4_MIN_RR or 3.0):
        funnel["S4_setup"] += 1
        sig = _profile_one(S, P, st, closed, cs, tf, a_ltf, a_h1, funnel, dec, kz, mode)
        if sig and _profile_gate(S, sig, provide, now, funnel):
            out.append(sig)
    return out
