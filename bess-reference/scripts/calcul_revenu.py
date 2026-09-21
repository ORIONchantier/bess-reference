"""Calcul des trois revenus de référence pour chaque journée collectée :
  - optimum à information parfaite (DA + aFRR capacité)
  - stratégie DA seul
  - stratégie aFRR seul
Programme linéaire (scipy, solveur HiGHS) au pas 15 min, contraintes physiques de config/parametres.json.
Écrit data/resultats/<jour>.json et met à jour data/index.json."""
import json, math, sys, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix
sys.path.insert(0, str(Path(__file__).resolve().parent))
import turpe as turpe_mod

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PARIS = ZoneInfo("Europe/Paris")
CFG = json.loads((ROOT / "config" / "parametres.json").read_text())
B, T = CFG["batterie"], CFG["turpe"]

P_KW = B["puissance_kw"]
E_KWH = B["capacite_kwh"]
SOC_MIN, SOC_MAX = B["soc_min"] * E_KWH, B["soc_max"] * E_KWH        # kWh
ETA = math.sqrt(B["rendement_aller_retour"])                            # par sens
CYCLES = B["cycles_max_par_jour"]
K = B.get("facteur_marge_afrr_k", 1.0)
BLOC = CFG.get("afrr", {}).get("bloc_reservation_min", 60)
DT = 0.25                                                               # h par pas
VERSION = 10                                                             # incrémenter force le recalcul des jours anciens


def instant(iso: str) -> int:
    """Clé de raccordement DA / aFRR : l'instant, pas la chaîne (RTE mélange UTC et heure de Paris)."""
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


TARIF = turpe_mod.TarifBT()          # remplacé par calculer() pour chaque scénario de raccordement


def charger_jour(jour: str):
    da = json.loads((DATA / "da" / f"{jour}.json").read_text())
    da = sorted(da, key=lambda p: p["debut"])
    cap_path = DATA / "afrr_capacite" / f"{jour}.json"
    cap = json.loads(cap_path.read_text()) if cap_path.exists() else []
    cap = [c for c in cap if str(c.get("horizon", "DAILY")).upper() == "DAILY"] or cap
    prix_cap = {}
    for c in cap:
        v = c.get("prix_eur_mw_15min", c.get("prix_eur_mw_h"))        # anciens fichiers : valeur brute dans _15min
        prix_cap[(instant(c["debut"]), c["sens"].upper())] = float(v)
    return da, prix_cap


def optimiser(da, prix_cap, avec_da=True, avec_afrr=True, reserves_fixes=None):
    """reserves_fixes : (ru_kw par bloc, rd_kw par bloc) pour une stratégie dont les réserves sont décidées d'avance."""
    n = len(da)
    if n == 0:
        return None
    debuts = [dt.datetime.fromisoformat(p["debut"]).astimezone(PARIS) for p in da]
    prix = np.array([float(p["prix"]) for p in da])                     # €/MWh
    turpe = np.array([TARIF.soutirage(t) for t in debuts])              # €/MWh soutirés
    turpe_i = np.array([TARIF.injection(t) for t in debuts])            # €/MWh injectés (HTA)
    pas_par_bloc = max(1, BLOC // 15)
    nb = math.ceil(n / pas_par_bloc)
    bloc_de = [i // pas_par_bloc for i in range(n)]
    def prix_bloc(sens):
        out = []
        for b in range(nb):
            vals = [prix_cap.get((instant(da[i]["debut"]), sens)) for i in range(b * pas_par_bloc, min(n, (b + 1) * pas_par_bloc))]
            vals = [v for v in vals if v is not None]
            out.append(float(np.mean(vals)) if vals else None)
        return out
    p_up, p_dn = prix_bloc("UP"), prix_bloc("DOWN")
    nb_raccordes = sum(v is not None for v in p_up + p_dn)
    afrr_ok = avec_afrr and nb_raccordes > 0
    p_up = [v if v is not None else 0.0 for v in p_up]
    p_dn = [v if v is not None else 0.0 for v in p_dn]

    # Variables : c[0..n) soutirage kW, d[n..2n) injection kW, s0 (SoC initial kWh),
    #             ru[..nb) réserve hausse kW par bloc, rd[..nb) réserve baisse kW par bloc
    ic, idx_d, i_s0 = 0, n, 2 * n
    i_ru, i_rd = 2 * n + 1, 2 * n + 1 + nb
    nv = 2 * n + 1 + 2 * nb
    cost = np.zeros(nv)                                                  # linprog minimise : on met -revenu
    if avec_da:
        cost[ic:ic + n] = (prix + turpe) * DT / 1000                     # coût d'achat + TURPE, € par kW
        cost[idx_d:idx_d + n] = -(prix - turpe_i) * DT / 1000
    if afrr_ok:
        cost[i_ru:i_ru + nb] = -np.array(p_up) * pas_par_bloc * DT / 1000   # €/MW/h * h / 1000 -> € par kW
        cost[i_rd:i_rd + nb] = -np.array(p_dn) * pas_par_bloc * DT / 1000

    A, bnd = lil_matrix((0, nv)), []
    rows = []
    def add(coefs, rhs):
        rows.append((coefs, rhs))
    # SoC après le pas t : s0 + sum_{k<=t} (c_k*ETA - d_k/ETA)*DT
    # marges aFRR : SoC_t - K*ru_b*1h >= SOC_MIN ; SoC_t + K*rd_b*1h <= SOC_MAX (pour chaque pas t du bloc b)
    for t in range(n):
        base = {i_s0: 1.0}
        for k in range(t + 1):
            base[ic + k] = ETA * DT
            base[idx_d + k] = -DT / ETA
        b = bloc_de[t]
        # -SoC_t + K*ru_b <= -SOC_MIN
        r = {j: -v for j, v in base.items()}; r[i_ru + b] = r.get(i_ru + b, 0) + K * 1.0
        add(r, -SOC_MIN)
        # SoC_t + K*rd_b <= SOC_MAX
        r = dict(base); r[i_rd + b] = r.get(i_rd + b, 0) + K * 1.0
        add(r, SOC_MAX)
    # puissance partagée : c_t + rd_b <= P ; d_t + ru_b <= P ; c_t + d_t <= P
    for t in range(n):
        b = bloc_de[t]
        add({ic + t: 1.0, i_rd + b: 1.0}, P_KW)
        add({idx_d + t: 1.0, i_ru + b: 1.0}, P_KW)
        add({ic + t: 1.0, idx_d + t: 1.0}, P_KW)
    # cycles : énergie déchargée côté batterie <= CYCLES * utile
    add({idx_d + t: DT / ETA for t in range(n)}, CYCLES * (SOC_MAX - SOC_MIN))
    # bilan journalier nul : SoC final = SoC initial  (sum c*ETA - d/ETA = 0)  -> deux inégalités
    add({**{ic + t: ETA * DT for t in range(n)}, **{idx_d + t: -DT / ETA for t in range(n)}}, 0.0)
    add({**{ic + t: -ETA * DT for t in range(n)}, **{idx_d + t: DT / ETA for t in range(n)}}, 0.0)

    A = lil_matrix((len(rows), nv))
    bvec = np.zeros(len(rows))
    for i, (coefs, rhs) in enumerate(rows):
        for j, v in coefs.items():
            A[i, j] = v
        bvec[i] = rhs
    bounds = [(0, P_KW if avec_da else 0)] * (2 * n) + [(SOC_MIN, SOC_MAX)] + \
             [(0, P_KW if afrr_ok else 0)] * (2 * nb)
    if reserves_fixes is not None:
        ru_f, rd_f = reserves_fixes
        for b in range(nb):
            bounds[i_ru + b] = (ru_f[b], ru_f[b])
            bounds[i_rd + b] = (rd_f[b], rd_f[b])
    res = linprog(cost, A_ub=A.tocsr(), b_ub=bvec, bounds=bounds, method="highs")
    if not res.success:
        return {"erreur": res.message, "prix_bloc_hausse": p_up, "prix_bloc_baisse": p_dn}
    x = res.x
    c, d, s0 = x[ic:ic + n], x[idx_d:idx_d + n], x[i_s0]
    ru, rd = x[i_ru:i_ru + nb], x[i_rd:i_rd + nb]
    soc = s0 + np.cumsum(c * ETA * DT - d * DT / ETA)
    rev_da_brut = float(np.sum((d - c) * prix) * DT / 1000)
    cout_turpe = float((np.sum(c * turpe) + np.sum(d * turpe_i)) * DT / 1000)
    rev_up = float(np.sum(ru * np.array(p_up)) * pas_par_bloc * DT / 1000)
    rev_dn = float(np.sum(rd * np.array(p_dn)) * pas_par_bloc * DT / 1000)
    mw = P_KW / 1000
    brut = rev_da_brut + rev_up + rev_dn
    return {
        "blocs_afrr_raccordes": int(nb_raccordes), "blocs_total": 2 * nb,
        "prix_bloc_hausse": p_up, "prix_bloc_baisse": p_dn, "pas_par_bloc": pas_par_bloc,
        "brut_eur": round(brut, 2), "net_eur": round(brut - cout_turpe, 2),
        "brut_eur_par_mw": round(brut / mw, 1), "net_eur_par_mw": round((brut - cout_turpe) / mw, 1),
        "da_brut_eur": round(rev_da_brut, 2), "turpe_eur": round(cout_turpe, 2),
        "afrr_hausse_eur": round(rev_up, 2), "afrr_baisse_eur": round(rev_dn, 2),
        "cycles": round(float(np.sum(d) * DT / ETA / (SOC_MAX - SOC_MIN)), 3),
        "soc_initial_kwh": round(float(s0), 1),
        "plan": [{"debut": da[t]["debut"], "soutirage_kw": round(float(c[t]), 1), "injection_kw": round(float(d[t]), 1),
                  "soc_kwh": round(float(soc[t]), 1), "reserve_hausse_kw": round(float(ru[bloc_de[t]]), 1),
                  "reserve_baisse_kw": round(float(rd[bloc_de[t]]), 1), "prix_da": float(prix[t]), "turpe": float(turpe[t]), "turpe_inj": float(turpe_i[t])}
                 for t in range(n)],
    }


def dans_plages(t: dt.datetime, plages) -> bool:
    hm = t.hour * 60 + t.minute
    for a, b in plages:
        ha, ma = map(int, a.split(":")); hb, mb = map(int, b.split(":"))
        if ha * 60 + ma <= hm < hb * 60 + mb:
            return True
    return False


def strategie_realiste(da, prix_cap):
    """Règle d'offre de 9h (config) confrontée au prix marginal publié, puis DA optimisé avec les réserves retenues."""
    R = CFG.get("strategie_realiste")
    if not R or not prix_cap:
        return None
    sonde = optimiser(da, prix_cap, True, True)          # pour récupérer les prix par bloc
    if not sonde or "prix_bloc_hausse" not in sonde:
        return None
    p_up, p_dn, ppb = sonde["prix_bloc_hausse"], sonde["prix_bloc_baisse"], sonde.get("pas_par_bloc", 4)
    debuts = [dt.datetime.fromisoformat(p["debut"]).astimezone(PARIS) for p in da]
    nb = len(p_up)
    ru, rd = [0.0] * nb, [0.0] * nb
    for b in range(nb):
        t = debuts[b * ppb]
        if dans_plages(t, R["hausse"]["plages"]) and p_up[b] >= R["hausse"]["prix_plancher_eur_mw_h"]:
            ru[b] = P_KW * R["hausse"]["part_puissance"]
        if dans_plages(t, R["baisse"]["plages"]) and p_dn[b] >= R["baisse"]["prix_plancher_eur_mw_h"]:
            rd[b] = P_KW * R["baisse"]["part_puissance"]
    out = optimiser(da, prix_cap, True, True, reserves_fixes=(ru, rd))
    if out and "erreur" not in out:
        out["regle"] = {"blocs_hausse_retenus": sum(1 for v in ru if v), "blocs_baisse_retenus": sum(1 for v in rd if v)}
    return out


def ex_post(jour: str, res: dict):
    """Complément aFRR énergie une fois la journée livrée. Proxy : part activée = taux national par pas 15 min.
    res : le dictionnaire d'un scénario (optimum, da_seul, afrr_seul, realiste)."""
    path = DATA / "afrr_energie" / f"{jour}.json"
    if not path.exists():
        return None
    en = json.loads(path.read_text())
    if not en:
        return None
    par_instant = {instant(e["debut"]): e for e in en}
    couverts = sum(1 for p in res["optimum"]["plan"] if instant(p["debut"]) in par_instant) if res.get("optimum") and res["optimum"].get("plan") else len(en)
    utile = SOC_MAX - SOC_MIN
    out = {}
    for nom in ("optimum", "afrr_seul", "realiste", "afrr_seul_100"):
        # afrr_seul_100 : mêmes réserves que aFRR seul, mais nos offres en énergie sont retenues à 100 % :
        # dès que RTE active dans un sens, toute la puissance réservée est activée (borne haute de l'énergie).
        plein = nom == "afrr_seul_100"
        o = res.get("afrr_seul" if plein else nom)
        if not o or o.get("erreur") or not o.get("plan"):
            continue
        plan = o["plan"]
        n = len(plan)
        soc_plan = [pt["soc_kwh"] for pt in plan]
        # bornes sur l'écart cumulé d'énergie dû aux activations, pour que le plan reste tenable jusqu'au soir
        suf_max = [0.0] * n; suf_min = [0.0] * n
        m1, m2 = -1e9, 1e9
        for t in range(n - 1, -1, -1):
            m1, m2 = max(m1, soc_plan[t]), min(m2, soc_plan[t]); suf_max[t], suf_min[t] = m1, m2
        budget_cycles = CYCLES * utile - sum(pt["injection_kw"] for pt in plan) * DT / ETA   # kWh batterie encore déchargeables
        rev_up = cout_dn = turpe = e_up_tot = e_dn_tot = ref_up = ref_dn = 0.0
        delta = 0.0; soc = []; manquants = 0; act_up_kw = []; act_dn_kw = []
        for t, pt in enumerate(plan):
            e = par_instant.get(instant(pt["debut"]))
            if e is None:
                manquants += 1; taux_up = taux_dn = 0.0; pu = pd = 0.0
            else:
                bu, bd = e.get("besoin_hausse_mw") or 0, abs(e.get("besoin_baisse_mw") or 0)
                taux_up = min(1.0, (e.get("active_hausse_mw") or 0) / bu) if bu else 0.0
                taux_dn = min(1.0, abs(e.get("active_baisse_mw") or 0) / bd) if bd else 0.0
                if plein:
                    taux_up = 1.0 if (e.get("active_hausse_mw") or 0) > 0 else 0.0
                    taux_dn = 1.0 if abs(e.get("active_baisse_mw") or 0) > 0 else 0.0
                pu, pd = e.get("prix_hausse_eur_mwh") or 0.0, e.get("prix_baisse_eur_mwh") or 0.0
                # règle d'offre en énergie indexée sur le spot du quart d'heure :
                # hausse activée seulement si le prix d'activation >= prix DA ; baisse seulement si prix d'activation <= prix DA
                spot = pt["prix_da"]
                if e.get("prix_hausse_eur_mwh") is None or pu < spot: taux_up = 0.0
                if e.get("prix_baisse_eur_mwh") is None or pd > spot: taux_dn = 0.0
            d_up = pt["reserve_hausse_kw"] * taux_up * DT              # kWh demandés à la hausse (côté réseau)
            d_dn = pt["reserve_baisse_kw"] * taux_dn * DT              # kWh demandés à la baisse
            # plafonds : SoC tenable sur le reste de la journée, et budget de cycles
            max_dn = max(0.0, (SOC_MAX - suf_max[t] - delta) / ETA)
            max_up = max(0.0, min((delta - (SOC_MIN - suf_min[t])) * ETA, budget_cycles * ETA))
            e_up, e_dn = min(d_up, max_up), min(d_dn, max_dn)
            ref_up += d_up - e_up; ref_dn += d_dn - e_dn
            rev_up += e_up / 1000 * pu
            cout_dn += e_dn / 1000 * pd
            turpe += e_dn / 1000 * pt["turpe"] + e_up / 1000 * pt.get("turpe_inj", 0.0)
            e_up_tot += e_up; e_dn_tot += e_dn
            budget_cycles -= e_up / ETA
            act_up_kw.append(round(e_up / DT, 1)); act_dn_kw.append(round(e_dn / DT, 1))
            delta += e_dn * ETA - e_up / ETA
            soc.append(round(soc_plan[t] + delta, 1))
        alertes = []
        if manquants and couverts >= 96: alertes.append(f"{manquants} pas sans donnée d'activation")
        cycles = (sum(pt["injection_kw"] for pt in plan) * DT + e_up_tot) / ETA / utile
        mw = P_KW / 1000
        # valeur de l'écart de SoC en fin de journée : surplus revendu, manque racheté, au prix DA moyen de la journée
        prix_moy_da = float(np.mean([pt["prix_da"] for pt in plan])) if plan else 0.0
        valeur_ecart = (delta * ETA if delta >= 0 else delta / ETA) / 1000 * prix_moy_da
        net = rev_up - cout_dn - turpe + valeur_ecart
        # règle : on ne dépose d'offre en énergie que si elle rapporte. Sinon, aucune activation ce jour-là
        # (offres au plafond), et on garde le montant qu'elle aurait coûté pour information.
        sans_offre = net < 0
        net_si_offre = net
        if sans_offre:
            net = 0.0; rev_up = cout_dn = turpe = e_up_tot = e_dn_tot = 0.0; valeur_ecart = 0.0; delta = 0.0
            ref_up = ref_dn = 0.0; act_up_kw = [0.0] * len(plan); act_dn_kw = [0.0] * len(plan); soc = list(soc_plan)
            cycles = sum(pt["injection_kw"] for pt in plan) * DT / ETA / utile
        out[nom] = {"energie_hausse_kwh": round(e_up_tot, 1), "energie_baisse_kwh": round(e_dn_tot, 1),
                    "refusee_hausse_kwh": round(ref_up, 1), "refusee_baisse_kwh": round(ref_dn, 1),
                    "revenu_hausse_eur": round(rev_up, 2), "cout_baisse_eur": round(cout_dn, 2), "turpe_eur": round(turpe, 2),
                    "complement_net_eur": round(net, 2), "complement_net_eur_par_mw": round(net / mw, 1),
                    "total_net_eur_par_mw": round(o["net_eur_par_mw"] + net / mw, 1),
                    "cycles_reels": round(cycles, 3), "ecart_soc_fin_kwh": round(delta, 1),
                    "valeur_ecart_soc_eur": round(valeur_ecart, 2), "prix_da_moyen_eur_mwh": round(prix_moy_da, 2),
                    "sans_offre_energie": bool(sans_offre), "net_si_offre_eur_par_mw": round(net_si_offre / mw, 1),
                    "soc_reel_kwh": soc, "active_hausse_kw": act_up_kw, "active_baisse_kw": act_dn_kw, "alertes": alertes,
                    "pas_couverts": int(couverts), "partiel": couverts < 96}
    return out or None


def calculer(jour: str) -> dict:
    global TARIF
    da, prix_cap = charger_jour(jour)
    out = {"jour": jour, "version": VERSION, "calcule_le": dt.datetime.now(PARIS).isoformat(timespec="minutes"),
           "puissance_mw": P_KW / 1000, "afrr_disponible": bool(prix_cap), "scenarios": {}}
    for cle, tarif in turpe_mod.scenarios():
        TARIF = tarif
        sc = {"tarif": tarif.description()}
        sc["optimum"] = optimiser(da, prix_cap, True, True)
        sc["da_seul"] = optimiser(da, prix_cap, True, False)
        sc["afrr_seul"] = optimiser(da, prix_cap, False, True)
        sc["realiste"] = strategie_realiste(da, prix_cap)
        out["scenarios"][cle] = sc
    TARIF = turpe_mod.TarifBT()
    return out


def main():
    jours = sorted(p.stem for p in (DATA / "da").glob("????-??-??.json"))
    demandes = [a for a in sys.argv[1:] if not a.startswith("--")]
    if demandes:
        jours = [j for j in jours if j in demandes]
    (DATA / "resultats").mkdir(exist_ok=True)
    force = "--force" in sys.argv
    for j in jours:
        cible = DATA / "resultats" / f"{j}.json"
        if cible.exists() and not force:
            try:
                if json.loads(cible.read_text()).get("version") == VERSION:
                    continue
            except Exception:
                pass
        r = calculer(j)
        cible.write_text(json.dumps(r, ensure_ascii=False))
        for cle, sc in r["scenarios"].items():
            o = sc["optimum"] or {}
            if r["afrr_disponible"] and not o.get("blocs_afrr_raccordes"):
                print(f"{j} {cle} : ATTENTION, prix aFRR présents mais aucun pas raccordé au DA (horodatages ?)")
            rl = sc.get("realiste") or {}
            print(f"{j} {cle} : optimum {o.get('net_eur_par_mw', '?')} €/MW net, DA seul {(sc['da_seul'] or {}).get('net_eur_par_mw', '?')}, "
                  f"aFRR seul {(sc['afrr_seul'] or {}).get('net_eur_par_mw', '?')}, réaliste {rl.get('net_eur_par_mw', rl.get('erreur', '?'))}, cycles {o.get('cycles', '?')}")
    # passe ex post : journées livrées dont les activations sont complètes
    for cible in sorted((DATA / "resultats").glob("????-??-??.json")):
        r = json.loads(cible.read_text())
        if r.get("version") != VERSION:
            continue
        modifie = False
        for cle, sc in r["scenarios"].items():
            deja = sc.get("ex_post") or {}
            if deja and not any(v.get("partiel") for v in deja.values()):
                continue                                   # définitif, rien à refaire
            try:
                xp = ex_post(cible.stem, sc)
            except Exception as err:
                print(f"{cible.stem} {cle} : estimation énergie impossible, {type(err).__name__}: {err}")
                continue
            if xp:
                sc["ex_post"] = xp; modifie = True
                o = xp.get("optimum", {})
                print(f"{cible.stem} {cle} : ex post énergie {'partiel ' + str(o.get('pas_couverts')) + '/96' if o.get('partiel') else 'définitif'}, "
                      f"optimum {o.get('complement_net_eur_par_mw', '?')} €/MW, réaliste {xp.get('realiste', {}).get('complement_net_eur_par_mw', '?')} €/MW")
        if modifie:
            cible.write_text(json.dumps(r, ensure_ascii=False))
    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx["resultats"] = sorted(p.stem for p in (DATA / "resultats").glob("????-??-??.json"))
    idx["scenarios"] = [{"cle": cle, **t.description()} for cle, t in turpe_mod.scenarios()]
    idx_path.write_text(json.dumps(idx, ensure_ascii=False))


if __name__ == "__main__":
    main()
