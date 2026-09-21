"""Modèle TURPE 7 (parties variables) pour la batterie de référence.
- BT > 36 kVA : grille CU4 à 4 plages (config turpe).
- HTA : poste source le plus proche -> zone HTA -> grille standard 5 plages, ou injection-soutirage
  (zone injection PV / zone soutirage) ; config turpe_hta. Logique reprise du classeur Fichier_Estimatif_TURPE_2026.xlsm.
Toutes les valeurs renvoyées sont en €/MWh (c€/kWh x 10)."""
import csv, json, math, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "parametres.json").read_text())

FERIES_FIXES = [(1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25)]


def paques(an: int) -> dt.date:
    a, b, c = an % 19, an // 100, an % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mois = (h + l - 7 * m + 114) // 31; jour = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(an, mois, jour)


def ferie(d: dt.date) -> bool:
    if (d.month, d.day) in FERIES_FIXES:
        return True
    p = paques(d.year)
    return d in (p + dt.timedelta(days=1), p + dt.timedelta(days=39), p + dt.timedelta(days=50))


def dans_heures(t: dt.datetime, plages) -> bool:
    hm = t.hour * 60 + t.minute
    for p in plages:
        a, b = p.split("-"); ha, ma = map(int, a.split(":")); hb, mb = map(int, b.split(":"))
        a_, b_ = ha * 60 + ma, hb * 60 + mb
        if (a_ <= hm < b_) if a_ < b_ else (hm >= a_ or hm < b_):
            return True
    return False


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_POSTES = None
def postes_sources():
    global _POSTES
    if _POSTES is None:
        _POSTES = []
        path = ROOT / CFG["turpe_hta"]["fichiers"]["postes_sources"]
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f, delimiter=";"):
                    try:
                        _POSTES.append({"code": r["code"], "nom": r["nom"], "lat": float(r["latitude"]), "lon": float(r["longitude"]),
                                        "zone_hta": r["zone_hta"], "zone_complete": r.get("zone_complete", "")})
                    except Exception:
                        pass
    return _POSTES


def poste_le_plus_proche(lat, lon):
    best, dmin = None, 1e9
    for p in postes_sources():
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d < dmin:
            best, dmin = p, d
    return (dict(best, distance_km=round(dmin, 2)) if best else None)


def grille_pour_zone(zone_hta: str) -> str:
    z = (zone_hta or "").lower()
    if "injection" in z and "hta" in z:
        return "zone_injection"
    if "soutirage" in z and "hta" in z:
        return "zone_soutirage"
    return "standard"


class TarifBT:
    """Grille BT > 36 kVA CU4 : soutirage 4 plages, injection 0."""
    type = "BT"
    def __init__(self):
        T = CFG["turpe"]
        self.s = T["soutirage_eur_mwh"]; self.inj = T.get("injection_eur_mwh", 0)
        self.saison_haute = T["saison_haute_mois"]
        a, b = T["heures_creuses"].split("-"); self.h0, self.h1 = int(a.split(":")[0]), int(b.split(":")[0])
        self.label = f"BT > 36 kVA, option {T['option']}"
    def plage(self, t):
        haute = t.month in self.saison_haute
        hc = (t.hour >= self.h0 or t.hour < self.h1) if self.h0 > self.h1 else (self.h0 <= t.hour < self.h1)
        return ("HC" if hc else "HP") + ("H" if haute else "B")
    def soutirage(self, t): return self.s[self.plage(t)]
    def injection(self, t): return self.inj
    def description(self): return {"type": "BT", "label": self.label, "grille": "CU4"}


class TarifHTA:
    """HTA : grille selon la zone du poste source le plus proche du site."""
    type = "HTA"
    def __init__(self, site: dict | None = None, zone_hta: str | None = None):
        H = CFG["turpe_hta"]; self.P = H["plages"]
        self.site = site; self.poste = None
        if site is not None and zone_hta is None:
            self.poste = poste_le_plus_proche(site["latitude"], site["longitude"])
            zone_hta = self.poste["zone_hta"] if self.poste else "Poste pas concernée par des contraintes locales de réseau"
        self.zone_hta = zone_hta or "Poste pas concernée par des contraintes locales de réseau"
        self.grille = grille_pour_zone(self.zone_hta)
        self.g = H["grilles_c_eur_kwh"][self.grille]
        self.label = f"HTA, {self.grille.replace('_', ' ')}"
    def plage(self, t):
        d = t.date(); we = d.weekday() == 6 or ferie(d)          # dimanche ou férié : HC ; samedi : selon heures
        samedi = d.weekday() == 5
        P = self.P
        if self.grille == "zone_injection":
            pi = P["pointe_injection"]
            if t.month in pi["mois"] and not we and dans_heures(t, pi["heures"]):
                return "POINTE_INJ"
        else:
            ps = P["pointe_soutirage"]
            if t.month in ps["mois"] and not we and dans_heures(t, ps["heures"]):
                return "POINTE"
        haute = t.month in P["saison_haute_mois"]
        hc = we or (samedi and P.get("week_end_et_feries_en_hc", True)) or dans_heures(t, [P["heures_creuses"]])
        return ("HC" if hc else "HP") + ("H" if haute else "B")
    def soutirage(self, t): return 10 * self.g["soutirage"][self.plage(t)]
    def injection(self, t): return 10 * self.g["injection"][self.plage(t)]
    def description(self):
        return {"type": "HTA", "label": self.label, "grille": self.grille, "zone_hta": self.zone_hta,
                "site": self.site["nom"] if self.site else None,
                "poste": {k: self.poste[k] for k in ("code", "nom", "distance_km")} if self.poste else None}


def sites_hta():
    path = ROOT / CFG["turpe_hta"]["fichiers"]["sites"]
    out = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if (r.get("type") or "").strip().upper() != "HTA":
                continue
            try:
                out.append({"nom": r["nom"].strip(), "latitude": float(str(r["latitude"]).replace(",", ".")),
                            "longitude": float(str(r["longitude"]).replace(",", ".")),
                            "puissance_kw": float(r.get("puissance_kw") or 0), "capacite_kwh": float(r.get("capacite_kwh") or 0)})
            except Exception:
                pass
    return out


def scenarios():
    """Liste des tarifs à calculer : BT de référence, HTA générique (poste non contraint), et un HTA par site du fichier."""
    out = [("BT", TarifBT()), ("HTA_generique", TarifHTA(zone_hta="Poste pas concernée par des contraintes locales de réseau"))]
    for s in sites_hta():
        cle = "HTA_" + "".join(ch if ch.isalnum() else "_" for ch in s["nom"])[:40]
        out.append((cle, TarifHTA(site=s)))
    return out


if __name__ == "__main__":
    import sys
    for cle, t in scenarios():
        print(cle, t.description())
