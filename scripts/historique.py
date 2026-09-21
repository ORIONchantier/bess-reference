"""Rattrapage historique : aFRR capacité et énergie via les API RTE, day-ahead France via ENTSO-E.
Usage : python scripts/historique.py 2025-01-01 2025-12-31
Écrit les mêmes fichiers que la collecte quotidienne (data/da, data/afrr_capacite, data/afrr_energie),
sans écraser un jour déjà présent sauf avec --force. Le calcul des revenus se fait ensuite par calcul_revenu.py."""
import os, sys, json, time, datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collecte_rte as C

PARIS = ZoneInfo("Europe/Paris")
DATA = C.DATA
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
ZONE_FR = "10YFR-RTE------C"


def jours(du: dt.date, au: dt.date):
    d = du
    while d <= au:
        yield d; d += dt.timedelta(days=1)


def existe(dossier, jour):
    return (DATA / dossier / f"{jour.isoformat()}.json").exists()


# ---------- DA via ENTSO-E
def entsoe_da(du: dt.date, au: dt.date, token: str) -> dict:
    """Renvoie {date: [points 15 min]} pour l'intervalle (appels par tranche d'un mois)."""
    out = {}
    debut = dt.datetime.combine(du, dt.time(0), PARIS)
    fin = dt.datetime.combine(au + dt.timedelta(days=1), dt.time(0), PARIS)
    cur = debut
    while cur < fin:
        nxt = min(fin, cur + dt.timedelta(days=31))
        params = {"securityToken": token, "documentType": "A44", "in_Domain": ZONE_FR, "out_Domain": ZONE_FR,
                  "periodStart": cur.astimezone(dt.timezone.utc).strftime("%Y%m%d%H%M"),
                  "periodEnd": nxt.astimezone(dt.timezone.utc).strftime("%Y%m%d%H%M")}
        r = requests.get(ENTSOE_URL, params=params, timeout=120)
        if r.status_code != 200:
            print(f"ENTSO-E {cur.date()} -> {nxt.date()} : HTTP {r.status_code}, {r.text[:200]}")
            cur = nxt; continue
        root = ET.fromstring(r.content)
        ns = {"n": root.tag.split("}")[0].strip("{")}
        for ts in root.findall("n:TimeSeries", ns):
            for per in ts.findall("n:Period", ns):
                start = dt.datetime.fromisoformat(per.find("n:timeInterval/n:start", ns).text.replace("Z", "+00:00"))
                res = per.find("n:resolution", ns).text
                pas = 60 if res == "PT60M" else 15 if res == "PT15M" else 30 if res == "PT30M" else None
                if pas is None:
                    continue
                pts = {}
                for p in per.findall("n:Point", ns):
                    pos = int(p.find("n:position", ns).text); prix = float(p.find("n:price.amount", ns).text)
                    pts[pos] = prix
                nb = max(pts) if pts else 0
                dernier = None
                for pos in range(1, nb + 1):
                    prix = pts.get(pos, dernier)          # positions manquantes = prix inchangé (règle ENTSO-E)
                    dernier = prix
                    t0 = start + dt.timedelta(minutes=pas * (pos - 1))
                    for q in range(pas // 15):             # horaire -> 4 quarts d'heure identiques
                        d0 = (t0 + dt.timedelta(minutes=15 * q)).astimezone(PARIS)
                        out.setdefault(d0.date().isoformat(), []).append({"debut": d0.isoformat(), "fin": (d0 + dt.timedelta(minutes=15)).isoformat(), "prix": prix, "volume_mw": None, "source": "ENTSO-E"})
        cur = nxt; time.sleep(1)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit("usage : historique.py AAAA-MM-JJ AAAA-MM-JJ [--force] [--sans-da] [--sans-afrr]")
    du, au = dt.date.fromisoformat(args[0]), dt.date.fromisoformat(args[1])
    force = "--force" in sys.argv
    token = C.get_token()

    if "--sans-afrr" not in sys.argv:
        # aFRR capacité : 20 jours par appel
        d = du
        while d <= au:
            f = min(au, d + dt.timedelta(days=19))
            manquants = [j for j in jours(d, f) if force or not existe("afrr_capacite", j)]
            if manquants:
                start = dt.datetime.combine(d, dt.time(0), PARIS); end = dt.datetime.combine(f + dt.timedelta(days=1), dt.time(0), PARIS)
                try:
                    cap = C.call(token, C.RES_AFRR_CAP, {"start_date": start.isoformat(timespec="seconds"), "end_date": end.isoformat(timespec="seconds")})
                    pts = C.parse_afrr_cap(cap)
                    par_jour = {}
                    for p in pts:
                        jj = dt.datetime.fromisoformat(p["debut"].replace("Z", "+00:00")).astimezone(PARIS).date().isoformat()
                        par_jour.setdefault(jj, []).append(p)
                    for jj, l in par_jour.items():
                        if force or not existe("afrr_capacite", dt.date.fromisoformat(jj)):
                            (DATA / "afrr_capacite").mkdir(exist_ok=True); (DATA / "afrr_capacite" / f"{jj}.json").write_text(json.dumps(l, ensure_ascii=False))
                    print(f"aFRR capacité {d} -> {f} : {len(pts)} valeurs, {len(par_jour)} jours")
                except Exception as e:
                    print(f"aFRR capacité {d} -> {f} : échec, {e}")
                time.sleep(1)
            d = f + dt.timedelta(days=1)
        # aFRR énergie : un mois par appel
        d = du
        while d <= au:
            f = min(au, (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
            manquants = [j for j in jours(d, f) if force or not existe("afrr_energie", j)]
            if manquants:
                start = dt.datetime.combine(d, dt.time(0), PARIS); end = dt.datetime.combine(f + dt.timedelta(days=1), dt.time(0), PARIS)
                try:
                    en = C.call(token, C.RES_AFRR_ENERGIE, {"start_date": start.isoformat(timespec="seconds"), "end_date": end.isoformat(timespec="seconds")})
                    pts = C.parse_afrr_energie(en)
                    par_jour = {}
                    for p in pts:
                        jj = dt.datetime.fromisoformat(p["debut"].replace("Z", "+00:00")).astimezone(PARIS).date().isoformat()
                        par_jour.setdefault(jj, []).append(p)
                    for jj, l in par_jour.items():
                        if force or not existe("afrr_energie", dt.date.fromisoformat(jj)):
                            (DATA / "afrr_energie").mkdir(exist_ok=True); (DATA / "afrr_energie" / f"{jj}.json").write_text(json.dumps(l, ensure_ascii=False))
                    print(f"aFRR énergie {d} -> {f} : {len(pts)} pas, {len(par_jour)} jours")
                except Exception as e:
                    print(f"aFRR énergie {d} -> {f} : échec, {e}")
                time.sleep(1)
            d = f + dt.timedelta(days=1)

    if "--sans-da" not in sys.argv:
        tok = os.environ.get("ENTSOE_TOKEN")
        if not tok:
            print("ENTSOE_TOKEN absent : day-ahead non rattrapé")
        else:
            manquants = [j for j in jours(du, au) if force or not existe("da", j)]
            if manquants:
                res = entsoe_da(manquants[0], manquants[-1], tok)
                n = 0
                for jj, l in res.items():
                    if du <= dt.date.fromisoformat(jj) <= au and (force or not existe("da", dt.date.fromisoformat(jj))):
                        l = sorted(l, key=lambda p: p["debut"])
                        if len(l) >= 92:                                   # journée complète (92 pas le jour du passage à l'heure d'été)
                            (DATA / "da").mkdir(exist_ok=True); (DATA / "da" / f"{jj}.json").write_text(json.dumps(l, ensure_ascii=False)); n += 1
                print(f"DA ENTSO-E : {n} jours écrits")
    C.rebuild_index()


if __name__ == "__main__":
    main()
