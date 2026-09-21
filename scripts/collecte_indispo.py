"""Indisponibilités de production (nucléaire par défaut) connues à l'heure limite de décision en J-1, au pas 15 min.
API Unavailability Additional Information v7, ressource generation_unavailabilities (guide v07.00.00, §6.1).

Chaque version d'indisponibilité porte sa publication_date (UTC). Pour la journée J on garde, par identifiant, la
dernière version publiée avant l'heure limite (section decision), on écarte les annulées (DISMISSED) et on somme la
capacité indisponible par quart d'heure. Aucune version postérieure n'est lue : l'historique est reconstitué sans
connaissance du futur. La courbe « dernière version » (tout ce qui est publié au moment du calcul) est gardée à côté
pour mesurer l'erreur de l'information disponible à 9h.

Usage : collecte_indispo.py                  -> J+1 (et recalcul tant que l'heure limite n'est pas passée)
        collecte_indispo.py 2025-01-01 2025-12-31   -> historique, appels par mois
Écrit data/indispo/<jour>.json (quarts d'heure) et <jour>.meta.json."""
import json, sys, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collecte_rte as C

PARIS = ZoneInfo("Europe/Paris")
UTC = dt.timezone.utc
Q = dt.timedelta(minutes=15)
CFG = json.loads((C.ROOT / "config" / "parametres.json").read_text())
IND = CFG.get("indisponibilites", {})
RES = IND.get("chemin", "unavailability_additional_information/v7/generation_unavailabilities")
FILIERES = IND.get("filieres", ["NUCLEAR"])
PAUSE = IND.get("pause_entre_requetes_historique_s", 180)          # guide : pas plus de 20 appels par heure
LIMITE = CFG.get("decision", {}).get("heure_limite_j_moins_1", "08:45")
DOSSIER = C.DATA / "indispo"


def lire_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def utc(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def bornes(jour: dt.date):
    return (dt.datetime.combine(jour, dt.time(0), PARIS).astimezone(UTC),
            dt.datetime.combine(jour + dt.timedelta(days=1), dt.time(0), PARIS).astimezone(UTC))


def limite_decision(jour: dt.date) -> dt.datetime:
    h, m = map(int, LIMITE.split(":"))
    return dt.datetime.combine(jour - dt.timedelta(days=1), dt.time(h, m), PARIS)


def versions(token: str, debut: dt.datetime, fin: dt.datetime):
    """Toutes les versions (last_version non renseigné) des indisponibilités dont la période touche [debut, fin[
    (date_type EVENT_DATE, GEUN-RG02). Pagination : code 206 + continuation_token, à renvoyer en entête dans les 60 s
    (GEUN-RG17/RG18). Le guide place le jeton « en entête de réponse » : on le cherche dans les entêtes puis dans le corps."""
    params = {"date_type": "EVENT_DATE", "start_date": utc(debut), "end_date": utc(fin), "fuel_type": ",".join(FILIERES)}
    out, jeton, pages, total = [], None, 0, None
    while True:
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if jeton:
            h["continuation_token"] = jeton
        for _ in range(3):
            r = requests.get(C.BASE_URL + RES, params=None if jeton else params, headers=h, timeout=120)
            if r.status_code != 429:
                break
            time.sleep(30)
        if r.status_code == 204:
            break
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code} : {r.text[:300]}")
        corps = r.json() if r.content else {}
        out += corps.get("generation_unavailabilities", [])
        pages += 1
        if total is None and isinstance(corps.get("total_match"), int):
            total = corps["total_match"]                                   # nombre total de versions (§6.1.1.3)
        jeton = (r.headers.get("continuation_token") or r.headers.get("continuation-token")
                 or corps.get("continuation_token"))
        if pages == 1:
            print(f"  HTTP {r.status_code}, total_match {total}, {len(out)} versions reçues, "
                  f"entêtes : {', '.join(k for k in r.headers if 'token' in k.lower() or 'range' in k.lower()) or 'aucun jeton'}")
        if r.status_code != 206:
            break
        if not jeton:
            print("  réponse partielle (206) sans jeton de continuation : résultat incomplet")
            break
        if pages >= 200:
            raise RuntimeError("pagination anormalement longue")
    if total is not None and len(out) < total:
        print(f"  INCOMPLET : {len(out)} versions lues sur {total} annoncées")
    return out, pages, total


def cle_version(v):
    n = str(v.get("version", ""))
    return (lire_iso(v["publication_date"]), int(n) if n.isdigit() else -1)


def connues(vs, instant):
    """Dernière version de chaque identifiant publiée à instant ou avant (instant None : toutes) ; annulées écartées."""
    par_id = {}
    for v in vs:
        if instant is not None and lire_iso(v["publication_date"]) > instant:
            continue
        k = v["identifier"]
        if k not in par_id or cle_version(v) > cle_version(par_id[k]):
            par_id[k] = v
    return [v for v in par_id.values() if v.get("event_status") != "DISMISSED"]


def courbe(evts, jour: dt.date):
    """MW indisponibles par quart d'heure, au prorata du recouvrement. Plusieurs déclarations sur la même unité
    (prévue + fortuite) sont sommées puis plafonnées à sa puissance installée."""
    d0, d1 = bornes(jour)
    qs = []
    t = d0
    while t < d1:
        qs.append(t); t += Q
    par_unite = {}
    for e in evts:
        u = e.get("affected_asset_or_unit_eic_code") or e["identifier"]
        cap = e.get("affected_asset_or_unit_installed_capacity")
        x = par_unite.setdefault(u, {"cap": cap, "type": e.get("affected_asset_or_unit_type"), "mw": [0.0] * len(qs)})
        for val in e.get("values") or []:
            v0, v1 = lire_iso(val["start_date"]), lire_iso(val["end_date"])
            mw = val.get("unavailable_capacity")
            if mw is None and cap is not None and val.get("available_capacity") is not None:
                mw = cap - val["available_capacity"]                     # GEUN-RG24
            if mw is None or v1 <= d0 or v0 >= d1:
                continue
            for i, q in enumerate(qs):
                rec = (min(v1, q + Q) - max(v0, q)).total_seconds()
                if rec > 0:
                    x["mw"][i] += mw * rec / Q.total_seconds()
    total = [0.0] * len(qs)
    par_type = {}
    for x in par_unite.values():
        for i, v in enumerate(x["mw"]):
            v = min(v, x["cap"]) if x["cap"] else v
            total[i] += v
        par_type[x["type"] or "?"] = par_type.get(x["type"] or "?", 0) + 1
    return qs, total, par_type


def calculer(vs, jour: dt.date, pages: int, total=None):
    lim = limite_decision(jour)
    maintenant = dt.datetime.now(UTC)
    ev_lim, ev_der = connues(vs, lim), connues(vs, None)
    qs, t_lim, types = courbe(ev_lim, jour)
    _, t_der, _ = courbe(ev_der, jour)
    lignes = [{"debut": q.astimezone(PARIS).isoformat(), "indispo_limite_mw": round(a, 1), "indispo_derniere_mw": round(b, 1)}
              for q, a, b in zip(qs, t_lim, t_der)]
    meta = {"jour": jour.isoformat(), "filieres": FILIERES, "limite_decision": lim.isoformat(),
            "provisoire": maintenant < lim, "calcule_le": maintenant.astimezone(PARIS).isoformat(timespec="minutes"),
            "versions_lues": len(vs), "pages": pages, "total_match": total,
            "complet": total is not None and len(vs) >= total,"evenements_a_la_limite": len(ev_lim), "evenements_derniere": len(ev_der),
            "unites_par_type": types,
            "moyenne_limite_mw": round(sum(t_lim) / len(t_lim), 0) if t_lim else None,
            "moyenne_derniere_mw": round(sum(t_der) / len(t_der), 0) if t_der else None}
    DOSSIER.mkdir(parents=True, exist_ok=True)
    (DOSSIER / f"{jour.isoformat()}.json").write_text(json.dumps(lignes, ensure_ascii=False))
    (DOSSIER / f"{jour.isoformat()}.meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    print(f"indispo {'+'.join(FILIERES)} {jour} : {meta['evenements_a_la_limite']} évènements connus à la limite, "
          f"moyenne {meta['moyenne_limite_mw']} MW (dernière version {meta['moyenne_derniere_mw']} MW), "
          f"unités par type {types}{', PROVISOIRE' if meta['provisoire'] else ''}")
    if len(types) > 1:
        print("  ATTENTION : déclarations sur plusieurs niveaux d'unité (GENERATION_UNIT et PRODUCTION_UNIT), risque de double compte")


def a_refaire(jour: dt.date) -> bool:
    m = DOSSIER / f"{jour.isoformat()}.meta.json"
    if not m.exists():
        return True
    try:
        meta = json.loads(m.read_text())
        # à refaire tant que l'heure limite n'est pas passée, ou si la lecture n'a pas été prouvée complète
        return bool(meta.get("provisoire", True)) or not meta.get("complet", False)
    except Exception:
        return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    token = C.get_token()
    if not args:
        demain = dt.datetime.now(PARIS).date() + dt.timedelta(days=1)
        if force or a_refaire(demain):
            d0, d1 = bornes(demain)
            vs, pages, total = versions(token, d0, d1)
            calculer(vs, demain, pages, total)
        else:
            print(f"indispo {demain} : déjà définitif (heure limite passée, lecture complète), rien à refaire")
    else:
        du, au = dt.date.fromisoformat(args[0]), dt.date.fromisoformat(args[-1])
        d = du
        while d <= au:
            f = min(au, (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
            jours = [d + dt.timedelta(days=i) for i in range((f - d).days + 1)]
            jours = [j for j in jours if force or a_refaire(j)]
            if jours:
                try:
                    vs, pages, total = versions(token, bornes(jours[0])[0], bornes(jours[-1])[1])
                    print(f"indispo {d} -> {f} : {len(vs)} versions sur {total} annoncées, {pages} page(s)")
                    for j in jours:
                        calculer(vs, j, pages, total)
                except Exception as e:
                    print(f"indispo {d} -> {f} : échec, {e}")
                if f < au:
                    time.sleep(PAUSE)
            d = f + dt.timedelta(days=1)
    idx_path = C.DATA / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx["indispo"] = sorted(p.stem for p in DOSSIER.glob("????-??-??.json"))
    idx_path.write_text(json.dumps(idx, ensure_ascii=False))


if __name__ == "__main__":
    main()
