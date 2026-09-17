"""Collecte quotidienne des prix J+1 sur les API RTE (day-ahead et aFRR capacité).
Écrit des fichiers JSON dans data/ que la page index.html lit ensuite.
Exécuté par GitHub Actions ; les identifiants viennent des variables d'environnement."""
import os, sys, json, base64, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

TOKEN_URL = "https://digital.iservices.rte-france.com/token/oauth/"
BASE_URL = "https://digital.iservices.rte-france.com/open_api/"
RES_DA = "wholesale_market/v2/france_power_exchanges"          # vérifié
RES_AFRR_CAP = os.environ.get("RTE_RES_AFRR_CAP", "")           # à coller depuis le guide RTE
PARIS = ZoneInfo("Europe/Paris")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def get_token() -> str:
    key = os.environ.get("RTE_KEY_B64")
    if not key:
        cid, sec = os.environ.get("RTE_CLIENT_ID"), os.environ.get("RTE_CLIENT_SECRET")
        if not (cid and sec):
            sys.exit("Identifiants RTE absents (RTE_KEY_B64 ou RTE_CLIENT_ID + RTE_CLIENT_SECRET)")
        key = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    r = requests.post(TOKEN_URL, headers={"Authorization": f"Basic {key}",
                      "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def call(token: str, resource: str, day: dt.date) -> dict:
    start = dt.datetime.combine(day, dt.time(0), PARIS)
    params = {"start_date": start.isoformat(timespec="seconds"),
              "end_date": (start + dt.timedelta(days=1)).isoformat(timespec="seconds")}
    for attempt in range(3):
        r = requests.get(BASE_URL + resource, params=params, timeout=60,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if r.status_code == 429:
            time.sleep(20); continue
        if r.status_code == 204:
            return {}
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"échec sur {resource}")


def save(folder: str, day: dt.date, payload: dict, points: list) -> None:
    d = DATA / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day.isoformat()}.raw.json").write_text(json.dumps(payload, ensure_ascii=False))
    if points:
        (d / f"{day.isoformat()}.json").write_text(json.dumps(points, ensure_ascii=False))


def parse_da(payload: dict) -> list:
    pts = []
    for block in payload.get("france_power_exchanges", []):
        for v in block.get("values", []):
            pts.append({"debut": v["start_date"], "fin": v["end_date"], "prix": v["price"]})
    return pts


def rebuild_index() -> None:
    jours = sorted(p.stem for p in (DATA / "da").glob("????-??-??.json"))
    (DATA / "index.json").write_text(json.dumps(
        {"jours": jours, "mis_a_jour": dt.datetime.now(PARIS).isoformat(timespec="minutes")}))


def main() -> None:
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today() + dt.timedelta(days=1)
    token = get_token()
    da_payload = call(token, RES_DA, day)
    da_pts = parse_da(da_payload)
    save("da", day, da_payload, da_pts)
    print(f"DA {day} : {len(da_pts)} pas de temps")
    if RES_AFRR_CAP:
        afrr = call(token, RES_AFRR_CAP, day)
        save("afrr_capacite", day, afrr, [])   # normalisation à écrire quand on aura un vrai retour
        print(f"aFRR capacité {day} : brut enregistré")
    else:
        print("aFRR capacité : ressource non renseignée, ignorée")
    rebuild_index()
    if not da_pts:
        sys.exit(2)   # RTE n'a pas encore publié : le workflow réessaiera


if __name__ == "__main__":
    main()
