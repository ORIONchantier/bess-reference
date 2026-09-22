"""Agent de décision avant la clôture aFRR capacité de J-1, approche 1 : règle adaptative.

Pour chaque jour de livraison D, l'agent choisit dans une grille de règles d'offre aFRR capacité (plages de baisse,
plages de hausse, part de la puissance, prix plancher) celle qui a le mieux rapporté sur les N derniers jours livrés et
complets jusqu'à D-2, avec score = moyenne - lambda x écart-type du revenu total par jour (capacité retenue au marginal,
DA optimisé ensuite avec les réserves figées, énergie aFRR activée, net TURPE : même séquence que la stratégie réaliste).
Il n'utilise que des prix et activations de journées déjà livrées : aucune donnée publiée après l'heure limite de D-1.

Trois étapes (paramètres : config/parametres.json, sections agent et decision) :
 1. évaluation : pour chaque journée complète, revenu de chaque règle de la grille -> data/agent/evaluations/<jour>.json
    (ne dépend pas de la date de décision : calculé une fois par jour et par version de grille) ;
 2. décision pour D -> data/agent/decisions/<D>.json. Recalculée à chaque passage tant que l'heure limite de D-1 n'est
    pas passée, puis figée. Les décisions des jours passés sont reconstituées (même règle de calcul, marquées comme telles) ;
 3. une fois D évalué : revenu réalisé de la règle choisie, comparé à la règle fixe, au DA seul, à la meilleure règle
    de la grille a posteriori et à l'optimum.

Usage : regle_adaptative.py            -> évalue au plus agent.evaluations_max_par_passage jours manquants, puis décide
        regle_adaptative.py --tout     -> évalue tout l'historique (long : workflow « Agent, rattrapage »)"""
import json, sys, math, hashlib, itertools, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
from scipy.optimize import linprog
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calcul_revenu as CR
import turpe as turpe_mod

PARIS = ZoneInfo("Europe/Paris")
DATA = CR.DATA
CFG = CR.CFG
A = CFG["agent"]
LIMITE = CFG.get("decision", {}).get("heure_limite_j_moins_1", "08:45")
DOSSIER = DATA / "agent"
P_KW, ETA, DT, K = CR.P_KW, CR.ETA, CR.DT, CR.K
SOC_MIN, SOC_MAX, CYCLES = CR.SOC_MIN, CR.SOC_MAX, CR.CYCLES
TARIF = dict(turpe_mod.scenarios()).get(A.get("scenario", "BT"), turpe_mod.TarifBT())

G = A["grille"]
REGLES = [{"baisse_plages": b, "hausse_plages": h, "part": p, "plancher": f}
          for b, h, p, f in itertools.product(G["baisse_plages"], G["hausse_plages"], G["parts"], G["planchers"])]
SIGNATURE = hashlib.sha1(json.dumps([G, A.get("scenario", "BT"), CFG["batterie"], CR.BLOC, CR.VERSION],
                                    sort_keys=True).encode()).hexdigest()[:12]


def decrire(r) -> str:
    pl = lambda x: ", ".join(f"{a}-{b}" for a, b in x) or "jamais"
    return f"baisse {pl(r['baisse_plages'])} ; hausse {pl(r['hausse_plages'])} ; {round(100 * r['part'])} % de la puissance ; plancher {r['plancher']} €/MW/h"


def indice(baisse, hausse, part, plancher):
    for i, r in enumerate(REGLES):
        if r["baisse_plages"] == baisse and r["hausse_plages"] == hausse and r["part"] == part and r["plancher"] == plancher:
            return i
    return None


RF = CFG.get("strategie_realiste", {})
I_FIXE = indice(RF.get("baisse", {}).get("plages"), RF.get("hausse", {}).get("plages"),
                RF.get("baisse", {}).get("part_puissance"), RF.get("baisse", {}).get("prix_plancher_eur_mw_h")) \
    if RF.get("baisse", {}).get("part_puissance") == RF.get("hausse", {}).get("part_puissance") else None
I_DA = indice([], [], G["parts"][0], G["planchers"][0])


# ---------- 1. évaluation d'une journée
def preparer(jour: str):
    """Matrices du programme linéaire de la journée, construites une fois : seules les bornes changent d'une règle à l'autre."""
    da, prix_cap = CR.charger_jour(jour)
    if not da or not prix_cap:
        return None
    n = len(da)
    debuts = [dt.datetime.fromisoformat(p["debut"]).astimezone(PARIS) for p in da]
    prix = np.array([float(p["prix"]) for p in da])
    turpe = np.array([TARIF.soutirage(t) for t in debuts]); turpe_i = np.array([TARIF.injection(t) for t in debuts])
    ppb = max(1, CR.BLOC // 15); nb = math.ceil(n / ppb); bloc_de = np.array([i // ppb for i in range(n)])
    def prix_bloc(sens):
        out = []
        for b in range(nb):
            v = [prix_cap.get((CR.instant(da[i]["debut"]), sens)) for i in range(b * ppb, min(n, (b + 1) * ppb))]
            v = [x for x in v if x is not None]
            out.append(float(np.mean(v)) if v else 0.0)
        return np.array(out)
    L = np.tril(np.ones((n, n)))
    S = np.hstack([L * ETA * DT, -L * DT / ETA, np.ones((n, 1))])                  # SoC après chaque pas
    A_ub = np.vstack([-S, S, np.hstack([np.eye(n), np.eye(n), np.zeros((n, 1))]),
                      np.concatenate([np.zeros(n), np.full(n, DT / ETA), [0.0]])[None, :]])
    A_eq = np.concatenate([np.full(n, ETA * DT), np.full(n, -DT / ETA), [0.0]])[None, :]
    cout = np.concatenate([(prix + turpe) * DT / 1000, -(prix - turpe_i) * DT / 1000, [0.0]])
    return {"da": da, "n": n, "debuts": debuts, "prix": prix, "turpe": turpe, "turpe_i": turpe_i, "ppb": ppb, "nb": nb,
            "bloc_de": bloc_de, "p_up": prix_bloc("UP"), "p_dn": prix_bloc("DOWN"), "A_ub": A_ub, "A_eq": A_eq, "cout": cout}


def reserves(prep, r):
    """Blocs retenus : dans les plages de la règle et prix marginal publié >= plancher (payé au marginal)."""
    ru, rd = np.zeros(prep["nb"]), np.zeros(prep["nb"])
    for b in range(prep["nb"]):
        t = prep["debuts"][b * prep["ppb"]]
        if CR.dans_plages(t, r["hausse_plages"]) and prep["p_up"][b] >= r["plancher"]:
            ru[b] = P_KW * r["part"]
        if CR.dans_plages(t, r["baisse_plages"]) and prep["p_dn"][b] >= r["plancher"]:
            rd[b] = P_KW * r["part"]
    return ru, rd


def resoudre(prep, ru, rd, jour):
    """DA optimisé avec réserves figées, puis énergie activée (même estimation que calcul_revenu.ex_post)."""
    n, bd = prep["n"], prep["bloc_de"]
    ru_t, rd_t = ru[bd], rd[bd]
    b_ub = np.concatenate([-(SOC_MIN + K * ru_t), SOC_MAX - K * rd_t, np.full(n, float(P_KW)), [CYCLES * (SOC_MAX - SOC_MIN)]])
    bornes = [(0, P_KW - x) for x in rd_t] + [(0, P_KW - x) for x in ru_t] + [(SOC_MIN, SOC_MAX)]
    res = linprog(prep["cout"], A_ub=prep["A_ub"], b_ub=b_ub, A_eq=prep["A_eq"], b_eq=[0.0], bounds=bornes, method="highs")
    if not res.success:
        return None
    x = res.x; c, d, s0 = x[:n], x[n:2 * n], x[2 * n]
    soc = s0 + np.cumsum(c * ETA * DT - d * DT / ETA)
    prix, turpe, turpe_i = prep["prix"], prep["turpe"], prep["turpe_i"]
    brut = float(np.sum((d - c) * prix) * DT / 1000) \
        + float((np.sum(ru * prep["p_up"]) + np.sum(rd * prep["p_dn"])) * prep["ppb"] * DT / 1000)
    cout_turpe = float((np.sum(c * turpe) + np.sum(d * turpe_i)) * DT / 1000)
    mw = P_KW / 1000
    o = {"net_eur_par_mw": round((brut - cout_turpe) / mw, 1),
         "plan": [{"debut": prep["da"][t]["debut"], "soutirage_kw": float(c[t]), "injection_kw": float(d[t]), "soc_kwh": float(soc[t]),
                   "reserve_hausse_kw": float(ru_t[t]), "reserve_baisse_kw": float(rd_t[t]), "prix_da": float(prix[t]),
                   "turpe": float(turpe[t]), "turpe_inj": float(turpe_i[t])} for t in range(n)]}
    xp = CR.ex_post(jour, {"realiste": o})
    xr = xp["realiste"] if xp and "realiste" in xp else None
    return o["net_eur_par_mw"], (xr["complement_net_eur_par_mw"] if xr else None), o, xr


def plan_agent(jour: str, dec: dict):
    """Plan de la batterie avec la règle choisie par l'agent (même format que les plans de calcul_revenu), pour la page."""
    prep = preparer(jour)
    if prep is None:
        return None
    ru, rd = reserves(prep, REGLES[dec["regle"]["indice"]])
    v = resoudre(prep, ru, rd, jour)
    if not v:
        return None
    _, _, o, xr = v
    plan = [{k: (p[k] if k == "debut" else round(p[k], 1)) for k in p} for p in o["plan"]]
    ex = {k: xr[k] for k in ("active_hausse_kw", "active_baisse_kw", "soc_reel_kwh", "cycles_reels", "complement_net_eur_par_mw",
                             "sans_offre_energie", "partiel") if k in xr} if xr else None
    cycles = sum(p["injection_kw"] for p in o["plan"]) * DT / ETA / (SOC_MAX - SOC_MIN)
    return {"plan": plan, "ex_post": ex, "cycles_plan": round(cycles, 3), "plan_pas_energie": pas_energie(jour),
            "plan_regle": dec["regle"]["indice"]}


def pas_energie(jour: str) -> int:
    p = DATA / "afrr_energie" / f"{jour}.json"
    try:
        return len(json.loads(p.read_text())) if p.exists() else 0
    except Exception:
        return 0


def evaluer(jour: str):
    prep = preparer(jour)
    (DOSSIER / "evaluations").mkdir(parents=True, exist_ok=True)
    if prep is None:
        # journée sans prix aFRR capacité : notée, pour ne pas bloquer les fenêtres qui la contiennent
        out = {"jour": jour, "signature": SIGNATURE, "absent": True, "complet": False, "pas_energie": pas_energie(jour),
               "capacite_da": [None] * len(REGLES), "total": [None] * len(REGLES)}
        (DOSSIER / "evaluations" / f"{jour}.json").write_text(json.dumps(out, ensure_ascii=False))
        print(f"agent évaluation {jour} : pas de prix aFRR capacité, journée exclue")
        return out
    nb_en = pas_energie(jour)
    complet = nb_en >= prep["n"]
    memo, cap_da, total = {}, [], []
    for r in REGLES:
        ru, rd = reserves(prep, r)
        cle = (ru.tobytes(), rd.tobytes())
        if cle not in memo:
            memo[cle] = resoudre(prep, ru, rd, jour)
        v = memo[cle]
        cap_da.append(v[0] if v else None)
        total.append(round(v[0] + v[1], 1) if v and v[1] is not None and complet else None)
    out = {"jour": jour, "signature": SIGNATURE, "scenario": A.get("scenario", "BT"), "nb_regles": len(REGLES),
           "programmes_resolus": len(memo), "pas_energie": nb_en, "complet": complet,
           "calcule_le": dt.datetime.now(PARIS).isoformat(timespec="minutes"), "capacite_da": cap_da, "total": total}
    (DOSSIER / "evaluations" / f"{jour}.json").write_text(json.dumps(out, ensure_ascii=False))
    # contrôle : la règle fixe évaluée ici doit redonner la stratégie réaliste de calcul_revenu (même scénario)
    ctrl = ""
    try:
        rr = json.loads((DATA / "resultats" / f"{jour}.json").read_text())["scenarios"][A.get("scenario", "BT")]["realiste"]
        if I_FIXE is not None and rr and cap_da[I_FIXE] is not None:
            ctrl = f", contrôle règle fixe {cap_da[I_FIXE]} contre {rr['net_eur_par_mw']} €/MW dans resultats"
    except Exception:
        pass
    print(f"agent évaluation {jour} : {len(REGLES)} règles, {len(memo)} programmes résolus, énergie {nb_en}/{prep['n']} pas{ctrl}")
    return out


def a_evaluer(jour: str) -> bool:
    p = DOSSIER / "evaluations" / f"{jour}.json"
    if not p.exists():
        return True
    try:
        e = json.loads(p.read_text())
    except Exception:
        return True
    if e.get("absent"):
        return (DATA / "afrr_capacite" / f"{jour}.json").exists() or e.get("signature") != SIGNATURE
    return e.get("signature") != SIGNATURE or (not e.get("complet") and pas_energie(jour) != e.get("pas_energie"))


# ---------- 2. décision
def limite_decision(jour: dt.date) -> dt.datetime:
    h, m = map(int, LIMITE.split(":"))
    return dt.datetime.combine(jour - dt.timedelta(days=1), dt.time(h, m), PARIS)


def charger_evaluations():
    ev = {}
    for p in sorted((DOSSIER / "evaluations").glob("????-??-??.json")):
        try:
            e = json.loads(p.read_text())
            if e.get("signature") == SIGNATURE:
                ev[e["jour"]] = e
        except Exception:
            pass
    return ev


def decider(D: dt.date, ev: dict, jours: list):
    """Fenêtre = les N derniers jours collectés jusqu'à D-2. Tous doivent avoir été évalués (sinon on attend : une décision
    figée sur une fenêtre partielle serait fausse) ; seuls ceux dont l'énergie est complète entrent dans le score."""
    N, lam = A.get("fenetre_jours", 30), A.get("lambda_ecart_type", 0.5)
    candidats = [j for j in jours if j <= (D - dt.timedelta(days=2)).isoformat()][-N:]
    if len(candidats) < N or any(j not in ev for j in candidats):
        return None
    fenetre = [j for j in candidats if ev[j]["complet"]]
    if len(fenetre) < A.get("fenetre_min_jours", 10):
        return None
    scores = []
    for i in range(len(REGLES)):
        v = [ev[j]["total"][i] for j in fenetre]
        if any(x is None for x in v):
            scores.append(None); continue
        m, s = float(np.mean(v)), float(np.std(v))
        scores.append({"i": i, "moyenne": round(m, 1), "ecart_type": round(s, 1), "score": round(m - lam * s, 1),
                       "p10": round(float(np.percentile(v, 10)), 1), "p90": round(float(np.percentile(v, 90)), 1)})
    valides = sorted([s for s in scores if s], key=lambda s: -s["score"])
    if not valides:
        return None
    best = valides[0]
    maintenant = dt.datetime.now(PARIS)
    return {"jour": D.isoformat(), "signature": SIGNATURE, "scenario": A.get("scenario", "BT"),
            "calcule_le": maintenant.isoformat(timespec="minutes"), "limite_decision": limite_decision(D).isoformat(),
            "reconstitue": maintenant > limite_decision(D), "fige": False,
            "fenetre": {"du": fenetre[0], "au": fenetre[-1], "nb_jours": len(fenetre), "lambda": lam},
            "regle": {**REGLES[best["i"]], "indice": best["i"], "texte": decrire(REGLES[best["i"]])},
            "attendu": {k: best[k] for k in ("moyenne", "ecart_type", "score", "p10", "p90")},
            "regle_fixe_attendu": scores[I_FIXE] if I_FIXE is not None else None,
            "da_seul_attendu": scores[I_DA] if I_DA is not None else None,
            "classement": [{"texte": decrire(REGLES[s["i"]]), **s} for s in valides[:5]]}


def realise(dec: dict, ev: dict):
    e = ev.get(dec["jour"])
    if not e:
        return None
    i = dec["regle"]["indice"]
    tot = [x for x in e["total"] if x is not None]
    out = {"complet": e["complet"], "capacite_da": e["capacite_da"][i], "total": e["total"][i],
           "regle_fixe": e["total"][I_FIXE] if I_FIXE is not None else None,
           "da_seul": e["total"][I_DA] if I_DA is not None else None,
           "meilleure_regle_a_posteriori": max(tot) if tot else None}
    try:
        sc = json.loads((DATA / "resultats" / f"{dec['jour']}.json").read_text())["scenarios"][dec["scenario"]]
        xp = (sc.get("ex_post") or {}).get("optimum")
        out["optimum"] = round(sc["optimum"]["net_eur_par_mw"] + (xp["complement_net_eur_par_mw"] if xp else 0), 1) if xp else None
    except Exception:
        out["optimum"] = None
    return out


def main():
    tout = "--tout" in sys.argv
    DOSSIER.mkdir(parents=True, exist_ok=True)
    jours = sorted(p.stem for p in (DATA / "da").glob("????-??-??.json"))
    manquants = [j for j in reversed(jours) if a_evaluer(j)]                  # les plus récents d'abord
    if not tout:
        manquants = manquants[:A.get("evaluations_max_par_passage", 5)]
    for j in manquants:
        try:
            evaluer(j)
        except Exception as err:
            print(f"agent évaluation {j} : échec, {type(err).__name__}: {err}")
    ev = charger_evaluations()
    if not ev:
        print("agent : aucune journée évaluée")
        return
    maintenant = dt.datetime.now(PARIS)
    demain = maintenant.date() + dt.timedelta(days=1)
    d0 = dt.date.fromisoformat(min(ev))
    cibles = [d0 + dt.timedelta(days=k) for k in range((demain - d0).days + 1)]
    (DOSSIER / "decisions").mkdir(exist_ok=True)
    nouvelles = 0
    for D in cibles:
        p = DOSSIER / "decisions" / f"{D.isoformat()}.json"
        dec = None
        if p.exists():
            try:
                dec = json.loads(p.read_text())
            except Exception:
                dec = None
        if dec and dec.get("signature") != SIGNATURE:
            dec = None                                                          # grille ou modèle changés : on refait
        if dec is None or (not dec.get("fige") and maintenant <= limite_decision(D)):
            neuve = decider(D, ev, jours)
            if neuve is None:
                continue
            dec = neuve; nouvelles += 1
        if not dec.get("fige") and maintenant > limite_decision(D):
            dec["fige"] = True                                                  # plus jamais recalculée
        r = realise(dec, ev)
        if r is not None:
            dec["realise"] = r
        e = ev.get(D.isoformat())
        # plan refait si absent, si de nouvelles activations sont arrivées, ou si la décision provisoire a changé de règle
        if e and not e.get("absent") and ("plan" not in dec or dec.get("plan_pas_energie") != e.get("pas_energie")
                                          or dec.get("plan_regle") != dec["regle"]["indice"]):
            try:
                pl = plan_agent(D.isoformat(), dec)
                if pl:
                    dec.update(pl)
            except Exception as err:
                print(f"agent plan {D} : échec, {type(err).__name__}: {err}")
        p.write_text(json.dumps(dec, ensure_ascii=False))
    decisions = sorted(p.stem for p in (DOSSIER / "decisions").glob("????-??-??.json"))
    # pourquoi pas de décision pour demain, le cas échéant (affiché sur la page)
    blocage = None
    if not (DOSSIER / "decisions" / f"{demain.isoformat()}.json").exists():
        cand = [j for j in jours if j <= (demain - dt.timedelta(days=2)).isoformat()][-A.get("fenetre_jours", 30):]
        non_eval = [j for j in cand if j not in ev]
        incomplets = [j for j in cand if j in ev and not ev[j]["complet"]]
        blocage = (f"fenêtre du {cand[0]} au {cand[-1]} : {len(non_eval)} jours pas encore évalués, "
                   f"{len(incomplets)} jours aux activations incomplètes" + (f" (dont {', '.join(incomplets[:3])}…)" if incomplets else "")) if cand else "historique insuffisant"
        print(f"agent : pas de décision pour {demain}, {blocage}")
    (DOSSIER / "index.json").write_text(json.dumps({"decisions": decisions, "signature": SIGNATURE, "blocage": blocage,
        "evaluees": len(ev), "evaluees_completes": sum(1 for e in ev.values() if e["complet"]),
        "nb_regles": len(REGLES), "fenetre_jours": A.get("fenetre_jours", 30), "lambda": A.get("lambda_ecart_type", 0.5),
        "scenario": A.get("scenario", "BT"), "mis_a_jour": maintenant.isoformat(timespec="minutes")}, ensure_ascii=False))
    dd = DOSSIER / "decisions" / f"{demain.isoformat()}.json"
    if dd.exists():
        x = json.loads(dd.read_text())
        print(f"agent décision {demain} ({'figée' if x['fige'] else 'provisoire jusqu à ' + LIMITE + ' J-1'}) : {x['regle']['texte']}, "
              f"attendu {x['attendu']['moyenne']} €/MW/j (score {x['attendu']['score']}), fenêtre {x['fenetre']['du']} -> {x['fenetre']['au']}")
    print(f"agent : {len(ev)} journées évaluées, {nouvelles} décisions calculées, {len(decisions)} au total")


if __name__ == "__main__":
    main()
