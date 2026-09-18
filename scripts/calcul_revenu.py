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


def turpe_eur_mwh(t: dt.datetime) -> float:
    """Composante de soutirage selon plage horosaisonnière (CU4, 4 plages)."""
    haute = t.month in T["saison_haute_mois"]
    h0, h1 = [int(x.split(":")[0]) for x in T["heures_creuses"].split("-")]
    hc = (t.hour >= h0 or t.hour < h1) if h0 > h1 else (h0 <= t.hour < h1)
    key = ("HC" if hc else "HP") + ("H" if haute else "B")
    return T["soutirage_eur_mwh"][key]


def charger_jour(jour: str):
    da = json.loads((DATA / "da" / f"{jour}.json").read_text())
    da = sorted(da, key=lambda p: p["debut"])
    cap_path = DATA / "afrr_capacite" / f"{jour}.json"
    cap = json.loads(cap_path.read_text()) if cap_path.exists() else []
    cap = [c for c in cap if str(c.get("horizon", "DAILY")).upper() == "DAILY"] or cap
    prix_cap = {}
    for c in cap:
        v = c.get("prix_eur_mw_15min", c.get("prix_eur_mw_h"))        # anciens fichiers : valeur brute dans _15min
        prix_cap[(c["debut"], c["sens"].upper())] = float(v)
    return da, prix_cap


def optimiser(da, prix_cap, avec_da=True, avec_afrr=True):
    n = len(da)
    if n == 0:
        return None
    debuts = [dt.datetime.fromisoformat(p["debut"]).astimezone(PARIS) for p in da]
    prix = np.array([float(p["prix"]) for p in da])                     # €/MWh
    turpe = np.array([turpe_eur_mwh(t) for t in debuts])                # €/MWh soutirés
    pas_par_bloc = max(1, BLOC // 15)
    nb = math.ceil(n / pas_par_bloc)
    bloc_de = [i // pas_par_bloc for i in range(n)]
    def prix_bloc(sens):
        out = []
        for b in range(nb):
            vals = [prix_cap.get((da[i]["debut"], sens)) for i in range(b * pas_par_bloc, min(n, (b + 1) * pas_par_bloc))]
            vals = [v for v in vals if v is not None]
            out.append(float(np.mean(vals)) if vals else None)
        return out
    p_up, p_dn = prix_bloc("UP"), prix_bloc("DOWN")
    afrr_ok = avec_afrr and any(v is not None for v in p_up + p_dn)
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
        cost[idx_d:idx_d + n] = -prix * DT / 1000
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
    res = linprog(cost, A_ub=A.tocsr(), b_ub=bvec, bounds=bounds, method="highs")
    if not res.success:
        return {"erreur": res.message}
    x = res.x
    c, d, s0 = x[ic:ic + n], x[idx_d:idx_d + n], x[i_s0]
    ru, rd = x[i_ru:i_ru + nb], x[i_rd:i_rd + nb]
    soc = s0 + np.cumsum(c * ETA * DT - d * DT / ETA)
    rev_da_brut = float(np.sum((d - c) * prix) * DT / 1000)
    cout_turpe = float(np.sum(c * turpe) * DT / 1000)
    rev_up = float(np.sum(ru * np.array(p_up)) * pas_par_bloc * DT / 1000)
    rev_dn = float(np.sum(rd * np.array(p_dn)) * pas_par_bloc * DT / 1000)
    mw = P_KW / 1000
    brut = rev_da_brut + rev_up + rev_dn
    return {
        "brut_eur": round(brut, 2), "net_eur": round(brut - cout_turpe, 2),
        "brut_eur_par_mw": round(brut / mw, 1), "net_eur_par_mw": round((brut - cout_turpe) / mw, 1),
        "da_brut_eur": round(rev_da_brut, 2), "turpe_eur": round(cout_turpe, 2),
        "afrr_hausse_eur": round(rev_up, 2), "afrr_baisse_eur": round(rev_dn, 2),
        "cycles": round(float(np.sum(d) * DT / ETA / (SOC_MAX - SOC_MIN)), 3),
        "soc_initial_kwh": round(float(s0), 1),
        "plan": [{"debut": da[t]["debut"], "soutirage_kw": round(float(c[t]), 1), "injection_kw": round(float(d[t]), 1),
                  "soc_kwh": round(float(soc[t]), 1), "reserve_hausse_kw": round(float(ru[bloc_de[t]]), 1),
                  "reserve_baisse_kw": round(float(rd[bloc_de[t]]), 1), "prix_da": float(prix[t]), "turpe": float(turpe[t])}
                 for t in range(n)],
    }


def calculer(jour: str) -> dict:
    da, prix_cap = charger_jour(jour)
    out = {"jour": jour, "calcule_le": dt.datetime.now(PARIS).isoformat(timespec="minutes"),
           "puissance_mw": P_KW / 1000, "afrr_disponible": bool(prix_cap)}
    out["optimum"] = optimiser(da, prix_cap, True, True)
    out["da_seul"] = optimiser(da, prix_cap, True, False)
    out["afrr_seul"] = optimiser(da, prix_cap, False, True)
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
            continue
        r = calculer(j)
        cible.write_text(json.dumps(r, ensure_ascii=False))
        o = r["optimum"] or {}
        print(f"{j} : optimum {o.get('net_eur_par_mw', '?')} €/MW net, DA seul {(r['da_seul'] or {}).get('net_eur_par_mw', '?')}, "
              f"aFRR seul {(r['afrr_seul'] or {}).get('net_eur_par_mw', '?')}, cycles {o.get('cycles', '?')}")
    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx["resultats"] = sorted(p.stem for p in (DATA / "resultats").glob("????-??-??.json"))
    idx_path.write_text(json.dumps(idx, ensure_ascii=False))


if __name__ == "__main__":
    main()
