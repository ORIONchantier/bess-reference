"""Collecte des prévisions RTE pour J+1 (et J) : consommation, production solaire et éolienne, charges résiduelles.
Ressources, séries et combinaisons dans config/parametres.json (section previsions).

Production : API Generation Forecast v3 (guide v03.01.01, §4.1). Pour le solaire et l'éolien, le guide ne décrit que
les échéances J-1 (appel vers 17h, échéance réglementaire 18h), infrajournalière et courante (mise à jour vers 9h30 et
19h30) ; J-2 et J-3 ne sont citées que pour l'agrégat OA et la MDSE, et une combinaison type/filière non prévue renvoie
l'erreur GENFORECAST_FORECASTS_F08. D'où :
  - D-1 solaire et éolien : la meilleure prévision, publiée APRÈS la clôture aFRR de 9h. Affichage, DA, pas l'agent.
  - CURRENT solaire et éolien : photographiée avant l'heure limite et figée ensuite (option figer_avant_limite).
    L'API ne garde que la dernière version : cet historique ne peut se construire qu'au fil de l'eau, à partir d'aujourd'hui.
  - AGGREGATED_CPC D-2 : agrégat des installations sous obligation d'achat, disponible la veille, historique possible.
Chaque valeur porte updated_date : <jour>.meta.json indique par série si elle était publiée avant l'heure limite."""
import json, sys, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collecte_rte as C

PARIS = ZoneInfo("Europe/Paris")
UTC = dt.timezone.utc
DATA = C.DATA
CFG = json.loads((C.ROOT / "config" / "parametres.json").read_text())
PREV = CFG.get("previsions", {})
LIMITE = CFG.get("decision", {}).get("heure_limite_j_moins_1", "08:45")


def lire_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def limite_decision(jour: dt.date) -> dt.datetime:
    h, m = map(int, LIMITE.split(":"))
    return dt.datetime.combine(jour - dt.timedelta(days=1), dt.time(h, m), PARIS)


def points(payload):
    """Parcours tolérant : tout objet avec start_date et value numérique, avec le type d'échéance et la filière hérités
    du bloc parent, et la date de mise à jour quand elle existe."""
    out = []
    def walk(x, ctx):
        if isinstance(x, dict):
            c = dict(ctx)
            c.update({k: x[k] for k in ("type", "sub_type", "production_type") if isinstance(x.get(k), str)})
            if "start_date" in x and isinstance(x.get("value"), (int, float)):
                out.append({**c, "debut": x["start_date"], "fin": x.get("end_date"), "valeur_mw": x["value"],
                            "maj": x.get("updated_date")})
            else:
                for v in x.values(): walk(v, c)
        elif isinstance(x, list):
            for v in x: walk(v, ctx)
    walk(payload, {})
    return out


def quarts(pts):
    """Étale au pas 15 min (une valeur horaire est répétée sur ses quatre quarts d'heure). Calcul en UTC pour les
    jours de changement d'heure. Clé : début du quart d'heure en heure de Paris (ISO)."""
    res = {}
    # versions successives d'une même échéance (sous-types DA01 puis DA02 de l'agrégat OA) : la plus récente l'emporte
    for p in sorted(pts, key=lambda p: lire_iso(p["maj"]) if p.get("maj") else dt.datetime.min.replace(tzinfo=UTC)):
        d0 = lire_iso(p["debut"]).astimezone(UTC)
        d1 = lire_iso(p["fin"]).astimezone(UTC) if p.get("fin") else d0 + dt.timedelta(minutes=15)
        t = d0
        while t < d1:
            res[t.astimezone(PARIS).isoformat()] = p["valeur_mw"]; t += dt.timedelta(minutes=15)
    return res


def serie(pts_ressource, spec):
    """Somme des filières demandées pour un type d'échéance. Une filière totalement absente du jour compte pour 0
    (noté dans le meta) ; une filière présente mais trouée rend le quart d'heure inconnu (None)."""
    typ, filieres = spec.get("type"), spec.get("production_type")
    choisis = [p for p in pts_ressource if (typ is None or p.get("type", typ) == typ)]      # sans champ type : pris tel quel
    if filieres:
        par_fil = {f: quarts([p for p in choisis if p.get("production_type") == f]) for f in filieres}
        absentes = [f for f, s in par_fil.items() if not s]
        presentes = {f: s for f, s in par_fil.items() if s}
        cles = set().union(*presentes.values()) if presentes else set()
        valeurs = {k: (None if any(s.get(k) is None for s in presentes.values()) else sum(s[k] for s in presentes.values()))
                   for k in cles}
        choisis = [p for p in choisis if p.get("production_type") in filieres]
    else:
        absentes, valeurs = [], quarts(choisis)
    majs = sorted((p["maj"] for p in choisis if p.get("maj")), key=lire_iso)
    return valeurs, {"nb_points": len(choisis), "maj_min": majs[0] if majs else None, "maj_max": majs[-1] if majs else None,
                     "filieres_absentes": absentes}


def detail_erreur(e: Exception) -> str:
    rep = getattr(e, "response", None)
    return f"{e}" + (f" | réponse : {rep.text[:400]}" if rep is not None and rep.text else "")


def collecter(token, jour: dt.date, save_raw=True):
    start = dt.datetime.combine(jour, dt.time(0), PARIS)
    params = {"start_date": start.isoformat(timespec="seconds"),
              "end_date": dt.datetime.combine(jour + dt.timedelta(days=1), dt.time(0), PARIS).isoformat(timespec="seconds")}
    brut, pts = {}, {}
    for nom, spec in PREV.get("ressources", {}).items():
        try:
            payload = C.call(token, spec["chemin"], {**params, **spec.get("parametres", {})})
            brut[nom], pts[nom] = payload, points(payload)
            print(f"prévision {nom} {jour} : {len(pts[nom])} valeurs")
            if not pts[nom]:
                print("  réponse brute :", json.dumps(payload)[:300])
        except Exception as e:
            print(f"prévision {nom} {jour} : échec, {detail_erreur(e)}")
    d = DATA / "previsions"
    limite = limite_decision(jour)
    maintenant = dt.datetime.now(PARIS)
    # fichier précédent : sert à garder les séries figées à l'heure limite
    try:
        prec_lignes = {l["debut"]: l for l in json.loads((d / f"{jour.isoformat()}.json").read_text())}
        prec_meta = json.loads((d / f"{jour.isoformat()}.meta.json").read_text()).get("series", {})
    except Exception:
        prec_lignes, prec_meta = {}, {}
    series, meta = {}, {"jour": jour.isoformat(), "limite_decision": limite.isoformat(), "series": {},
                        "calcule_le": maintenant.isoformat(timespec="minutes")}
    for nom, spec in PREV.get("series", {}).items():
        valeurs, m = serie(pts[spec["ressource"]], spec) if spec["ressource"] in pts else ({}, None)
        if m is not None:
            m["avant_limite"] = (lire_iso(m["maj_max"]) <= limite) if m["maj_max"] else None      # None : pas de date publiée
            if m["avant_limite"] is None and spec.get("figer_avant_limite") and m["nb_points"]:
                m["avant_limite"] = maintenant <= limite                  # à défaut, l'heure de la photographie
            if not m["nb_points"]:
                m["avant_limite"] = None                                  # rien reçu : rien à figer
        if spec.get("figer_avant_limite"):
            p = prec_meta.get(nom)
            if p and p.get("avant_limite") and not (m and m["avant_limite"]):
                # la photographie prise avant la limite est conservée ; la version plus récente est ignorée
                valeurs = {k: l.get(nom + "_mw") for k, l in prec_lignes.items()}
                m = dict(p, fige=True)
            elif maintenant > limite and not (p and p.get("avant_limite")) and not (m and m["avant_limite"]):
                m = dict(m or {}, indisponible_a_la_limite=True)      # aucun passage avant la limite ce jour-là
                valeurs = {}
        if m is None:
            continue
        series[nom], meta["series"][nom] = valeurs, m
        if m.get("filieres_absentes"):
            print(f"  {nom} {jour} : filières sans valeur, comptées à 0 : {', '.join(m['filieres_absentes'])}")
    if not any(series.values()):
        return False
    cles = sorted(set().union(*[set(s) for s in series.values()]), key=lambda k: lire_iso(k).astimezone(UTC))
    lignes = []
    for k in cles:
        l = {"debut": k}
        for nom in series: l[nom + "_mw"] = series[nom].get(k)
        for nom_cr, (c_, s_, e_) in PREV.get("charges_residuelles", {}).items():
            conso, sol, eol = l.get(c_ + "_mw"), l.get(s_ + "_mw"), l.get(e_ + "_mw")
            # une composante manquante rend la charge résiduelle inconnue : pas de zéro implicite
            l[nom_cr + "_mw"] = conso - sol - eol if None not in (conso, sol, eol) else None
        lignes.append(l)
    d.mkdir(exist_ok=True)
    (d / f"{jour.isoformat()}.json").write_text(json.dumps(lignes, ensure_ascii=False))
    (d / f"{jour.isoformat()}.meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    if save_raw:
        (d / f"{jour.isoformat()}.raw.json").write_text(json.dumps(brut, ensure_ascii=False))
    for nom, m in meta["series"].items():
        print(f"  {nom} {jour} : mise à jour {m.get('maj_min')} -> {m.get('maj_max')}, avant la limite de {LIMITE} J-1 : "
              f"{m.get('avant_limite')}{', figée' if m.get('fige') else ''}")
    return True


def main():
    token = C.get_token()
    today = dt.datetime.now(PARIS).date()
    cibles = [dt.date.fromisoformat(a) for a in sys.argv[1:] if not a.startswith("--")] or [today, today + dt.timedelta(days=1)]
    for j in cibles:
        collecter(token, j)
    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx["previsions"] = sorted(p.stem for p in (DATA / "previsions").glob("????-??-??.json"))
    idx_path.write_text(json.dumps(idx, ensure_ascii=False))


if __name__ == "__main__":
    main()
