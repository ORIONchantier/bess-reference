"""Collecte quotidienne des prix J+1 sur les API RTE (day-ahead et aFRR capacité).
Écrit des fichiers JSON dans data/ que la page index.html lit ensuite.
Exécuté par GitHub Actions ; les identifiants viennent des variables d'environnement."""
import os, sys, json, base64, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

TOKEN_URL = "https://digital.iservices.rte-france.com/token/oauth/"
BASE_URL = "https://digital.iservices.rte-france.com/open_api/"
RES_DA = "wholesale_market/v3/france_power_exchanges"              # guide Wholesale Market v3.0
RES_AFRR_CAP = "balancing_capacity/v5/result_procured_reserves"    # guide Balancing Capacity v5.0.3
RES_AFRR_ENERGIE = "balancing_energy/v5/standard_afrr_data"        # guide Balancing Energy v5.2.2
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


def parse_afrr_cap(payload: dict) -> list:
    """Guide v5 : result_procured_reserves[] avec reserve, product_type, values[] par pas 15 min.
    Le prix RTE est en €/MW/15 min ; on ajoute la conversion en €/MW/h (x4)."""
    pts = []
    for block in payload.get("result_procured_reserves", []):
        if block.get("reserve") != "AFRR":
            continue
        for v in block.get("values", []):
            pts.append({"debut": v["start_date"], "fin": v["end_date"], "sens": v["direction"],
                        "prix_eur_mw_15min": v["price"], "prix_eur_mw_h": round(4 * v["price"], 4),
                        "offert_mw": v.get("offered_volume"), "retenu_mw": v.get("contracted_volume"),
                        "horizon": v.get("time_horizon"), "produit": block.get("product_type")})
    return pts


def parse_afrr_energie(payload: dict) -> list:
    """Guide v5.2.2 : picasso.picasso_mesures[] par pas 15 min, volumes MW et prix moyens pondérés €/MWh."""
    pts = []
    for m in payload.get("picasso", {}).get("picasso_mesures", []):
        pts.append({"debut": m["start_date"], "fin": m["end_date"],
                    "besoin_hausse_mw": m.get("upward_afrr_requested_need"),
                    "besoin_baisse_mw": m.get("downward_afrr_requested_need"),
                    "active_hausse_mw": m.get("upward_afrr_activated_volume_for_fr"),
                    "active_baisse_mw": m.get("downward_afrr_activated_volume_for_fr"),
                    "prix_hausse_eur_mwh": m.get("upward_weighted_average_price_afrr_activated_for_fr"),
                    "prix_baisse_eur_mwh": m.get("downward_weighted_average_price_afrr_activated_for_fr")})
    return pts


def bornes(day: dt.date) -> dict:
    start = dt.datetime.combine(day, dt.time(0), PARIS)
    return {"start_date": start.isoformat(timespec="seconds"),
            "end_date": (start + dt.timedelta(days=1)).isoformat(timespec="seconds")}


def rebuild_index() -> None:
    jours = sorted(p.stem for p in (DATA / "da").glob("????-??-??.json"))
    cap = sorted(p.stem for p in (DATA / "afrr_capacite").glob("????-??-??.json"))
    en = sorted(p.stem for p in (DATA / "afrr_energie").glob("????-??-??.json"))
    (DATA / "index.json").write_text(json.dumps(
        {"jours": jours, "afrr_capacite": cap, "afrr_energie": en,
         "mis_a_jour": dt.datetime.now(PARIS).isoformat(timespec="minutes")}))


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

    # aFRR capacité pour J+1 : résultats de l'appel d'offres publiés vers 9h30 en J.
    try:
        cap = call(token, RES_AFRR_CAP, bornes(attendu))
        cap_pts = parse_afrr_cap(cap)
        save("afrr_capacite", attendu, cap, cap_pts)
        print(f"aFRR capacité {attendu} : {len(cap_pts)} valeurs (pas 15 min x sens)")
        if not cap_pts:
            print("  réponse brute :", json.dumps(cap)[:500])
    except Exception as e:
        print("aFRR capacité : échec,", e)

    # aFRR énergie pour J-1 : activations réelles, pour le complément ex post.
    hier = today - dt.timedelta(days=1)
    try:
        en = call(token, RES_AFRR_ENERGIE, bornes(hier))
        en_pts = parse_afrr_energie(en)
        save("afrr_energie", hier, en, en_pts)
        print(f"aFRR énergie {hier} : {len(en_pts)} pas de 15 min")
        if not en_pts:
            print("  réponse brute :", json.dumps(en)[:500])
    except Exception as e:
        print("aFRR énergie : échec,", e)

    rebuild_index()
    if day != attendu:
        print("RTE n'a pas encore basculé sur J+1 : le workflow réessaiera plus tard")
        sys.exit(2)


if __name__ == "__main__":
    main()
