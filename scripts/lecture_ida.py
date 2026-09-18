"""Lecture des résultats des enchères intraday EPEX (SIDC IDA1/IDA2/IDA3, zone France) déposés à la main
dans data/ida sous la forme AAAA-MM-JJ-IDAn.txt (texte collé depuis la page des résultats EPEX).
Produit data/ida/AAAA-MM-JJ-IDAn.json au pas 15 min et met à jour data/index.json.
Usage interne Orion Energies : données EPEX SPOT SE, pas de redistribution."""
import json, re, sys, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IDA = DATA / "ida"
PARIS = ZoneInfo("Europe/Paris")

RE_NOM = re.compile(r"(\d{4}-\d{2}-\d{2}).*?(IDA\s?[123])", re.I)
RE_CRENEAU = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
RE_NOMBRE = r"-?\d+(?:[.,]\d+)?"
RE_LIGNE = re.compile(rf"^\s*({RE_NOMBRE})\s+({RE_NOMBRE})\s+({RE_NOMBRE})\s+({RE_NOMBRE})\s*$")


def nombre(s: str) -> float:
    return float(s.replace(",", "."))


def lire_texte(texte: str):
    creneaux, lignes = [], []
    for raw in texte.splitlines():
        m = RE_CRENEAU.match(raw)
        if m:
            creneaux.append(tuple(int(x) for x in m.groups())); continue
        m = RE_LIGNE.match(raw.replace("\t", " "))
        if m:
            lignes.append(tuple(nombre(x) for x in m.groups()))
    if not creneaux or not lignes:
        raise ValueError(f"{len(creneaux)} créneaux et {len(lignes)} lignes de valeurs trouvés")
    if len(creneaux) != len(lignes):
        raise ValueError(f"{len(creneaux)} créneaux pour {len(lignes)} lignes de valeurs : le collage est incomplet ou mélangé")
    return creneaux, lignes


def convertir(path: Path) -> dict:
    m = RE_NOM.search(path.stem)
    if not m:
        raise ValueError("nom de fichier attendu : AAAA-MM-JJ-IDA1.txt (ou IDA2, IDA3)")
    jour = dt.date.fromisoformat(m.group(1))
    enchere = m.group(2).upper().replace(" ", "")
    creneaux, lignes = lire_texte(path.read_text(encoding="utf-8", errors="replace"))
    pts = []
    for (h1, m1, h2, m2), (achat, vente, volume, prix) in zip(creneaux, lignes):
        debut = dt.datetime.combine(jour, dt.time(0), PARIS) + dt.timedelta(hours=h1, minutes=m1)
        fin = dt.datetime.combine(jour, dt.time(0), PARIS) + dt.timedelta(hours=h2, minutes=m2)
        pts.append({"debut": debut.isoformat(), "fin": fin.isoformat(), "prix": prix,
                    "volume_mwh": volume, "achat_mwh": achat, "vente_mwh": vente})
    attendu = 48 if enchere == "IDA3" else 96
    return {"jour": jour.isoformat(), "enchere": enchere, "zone": "FR", "source": "EPEX SPOT, collage manuel",
            "nb_pas": len(pts), "complet": len(pts) == attendu, "points": pts}


def main():
    IDA.mkdir(exist_ok=True)
    rapport = {}
    for path in sorted(IDA.glob("*.txt")):
        cible = path.with_suffix(".json")
        if cible.exists() and cible.stat().st_mtime >= path.stat().st_mtime and "--force" not in sys.argv:
            continue
        try:
            res = convertir(path)
            cible.write_text(json.dumps(res, ensure_ascii=False))
            print(f"{path.name} : {res['enchere']} du {res['jour']}, {res['nb_pas']} pas{'' if res['complet'] else ' (INCOMPLET)'}")
        except Exception as e:
            rapport[path.name] = str(e)
            print(f"{path.name} : ILLISIBLE, {e}")
    index = {}
    for js in sorted(IDA.glob("*.json")):
        try:
            r = json.loads(js.read_text())
            index.setdefault(r["jour"], []).append(r["enchere"])
        except Exception:
            pass
    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx["ida"] = {k: sorted(v) for k, v in index.items()}
    idx["ida_illisibles"] = rapport
    idx_path.write_text(json.dumps(idx, ensure_ascii=False))


if __name__ == "__main__":
    main()
