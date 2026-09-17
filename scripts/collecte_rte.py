"""Collecte quotidienne des prix J+1 sur les API RTE (day-ahead et aFRR capacité).
Écrit des fichiers JSON dans data/ que la page index.html lit ensuite.
Exécuté par GitHub Actions ; les identifiants viennent des variables d'environnement."""
import os, sys, json, base64, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

TOKEN_URL = "https://digital.iservices.rte-france.com/token/oauth/"
BASE_URL = "https://digital.iservices.rte-france.com/open_api/"
RES_DA = os.environ.get("RTE_RES_DA", "wholesale_market/v3/france_power_exchanges")  # v3 : chemin à confirmer dans le guide
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


def call(token: str, resource: str, params: dict | None = None) -> dict:
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


def parse_da(payload: dict):
    """Structure du guide v3 : france_power_exchanges[0].values[] avec start_date, end_date, value (MW), price (€/MWh).
    Retourne (journée couverte, liste de points)."""
    blocks = payload.get("france_power_exchanges", [])
    if not blocks:
        return None, []
    b = blocks[0]
    day = dt.datetime.fromisoformat(b["start_date"]).date()
    pts = [{"debut": v["start_date"], "fin": v["end_date"], "prix": v["price"], "volume_mw": v.get("value")}
           for v in b.get("values", [])]
    return day, pts


def rebuild_index() -> None:
    jours = sorted(p.stem for p in (DATA / "da").glob("????-??-??.json"))
    (DATA / "index.json").write_text(json.dumps(
        {"jours": jours, "mis_a_jour": dt.datetime.now(PARIS).isoformat(timespec="minutes")}))


def main() -> None:
    today = dt.datetime.now(PARIS).date()
    attendu = today + dt.timedelta(days=1)
    token = get_token()

    # Day-ahead : l'API v3 n'a pas de paramètre, elle renvoie J avant 13h et J+1 après 14h (guide, règle ESP-RG03).
    da_payload = call(token, RES_DA)
    day, da_pts = parse_da(da_payload)
    if day is None or not da_pts:
        print("DA : réponse vide. 500 premiers caractères :", json.dumps(da_payload)[:500])
        sys.exit(2)
    save("da", day, da_payload, da_pts)
    print(f"DA : journée {day}, {len(da_pts)} pas de temps (attendu {attendu})")

    if RES_AFRR_CAP:
        start = dt.datetime.combine(attendu, dt.time(0), PARIS)
        afrr = call(token, RES_AFRR_CAP, {"start_date": start.isoformat(timespec="seconds"),
                                          "end_date": (start + dt.timedelta(days=1)).isoformat(timespec="seconds")})
        save("afrr_capacite", attendu, afrr, [])   # normalisation à écrire quand on aura un vrai retour
        print(f"aFRR capacité {attendu} : brut enregistré")
    else:
        print("aFRR capacité : ressource non renseignée, ignorée")

    rebuild_index()
    if day != attendu:
        print("RTE n'a pas encore basculé sur J+1 : le workflow réessaiera plus tard")
        sys.exit(2)


if __name__ == "__main__":
    main()
