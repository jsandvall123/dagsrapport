"""Daglig rapport: annonskostnad, omsättning och vinst för alla butiker.

Körs varje morgon av GitHub Actions (se .github/workflows/daily.yml).
1. Kampanjkontroll: varje kampanj i det delade Meta-kontot räknas bara i rätt butik i Juicy.
2. Hämtar P&L per dag + topp 5 annonser efter spend från Juicy (7 dagar t.o.m. igår).
3. Räknar om DKK/NOK/EUR till SEK med Riksbankens dagskurs.
4. Skriver site/index.html (krypteras med lösenord i nästa steg av workflowen).

Nycklar läses ur miljövariabler JUICY_<KOD>_TOKEN (GitHub secrets).
Lokalt: python3 build.py --env ../.secrets/shopify-stores.env [--dry]
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

JUICY = "https://juicy.easyapps.cloud"
STORES = [  # ordning i rapporten
    ("SE", "isella.se"), ("DK", "isella.dk"), ("NO", "isella.no"), ("FI", "isella.fi"),
    ("DE", "isellawear.de"), ("HOMELYST", "Homelyst"), ("HUSTOVALD", "Hus Tovald"),
    ("JUNO", "Juno"), ("EARLO", "Earlo"),
]
PREFIX = {"DK": "DK//ISELLA.DK//", "NO": "NO//ISELLA.NO//", "FI": "FI//ISELLA.FI//", "DE": "DE//ISELLAWEAR.DE//"}
PRODUCT_STORES = ["HOMELYST"]  # butiker med flera produkter: visas även per produkt och land
DAYS_SV = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MONTHS_SV = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
HERE = os.path.dirname(os.path.abspath(__file__))


def owner(name, created):
    """Vilken butik en kampanj tillhör. Samma regel som shopify-mcp/juicy_sync.py."""
    u = name.upper()
    if "HOMELYST" in u:
        return "HOMELYST"
    if "TOVALD" in u:
        return "HUSTOVALD"
    if "JUNO" in u:
        return "JUNO"
    if "EARLO" in u:
        return "EARLO"
    for code, p in PREFIX.items():
        if name.startswith(p):
            return code
    if created < "2025-10-09" or name.startswith("US//ISELLAWEAR"):
        return None
    return "SE"


def product_of(campaign):
    """'DK//HOMELYST//JELLYFISH LAMP/10.11' -> ('Jellyfish Lamp', 'DK'). Kampanjer utan produkt -> 'Flera produkter'."""
    parts = [p for p in (campaign or "").split("//")]
    land = parts[0] if len(parts) > 1 and len(parts[0]) == 2 else "?"
    prod = parts[2].split("/")[0].strip() if len(parts) > 2 else ""
    if not prod or prod.upper() in ("ALL PRODUCTS", "BLACK FRIDAY"):
        prod = "Flera produkter"
    return prod.title() if prod != "Flera produkter" else prod, land


def http(url, token=None, data=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return json.load(urllib.request.urlopen(req, timeout=90))


def juicy(token, path, data=None):
    return http(JUICY + path, token, data)


def sync_campaigns(tokens, dry):
    """Rättar ignore-listan i varje butik. Returnerar ändringar + nya kampanjer senaste 3 dagarna."""
    changes, seen, errs = [], {}, []
    for code, tok in tokens.items():
        try:  # en butik som felar (t.ex. Juicy-abonnemang slut) ska inte stoppa kontrollen i de andra
            camps = juicy(tok, "/api/facebook/credentials")["campaigns"]
        except Exception as e:
            errs.append(f"{code}: {e}")
            continue
        entries = []
        for c in camps:
            seen[c["campaignId"]] = (c["name"], c["createdAt"][:10])
            want_ignored = owner(c["name"], c["createdAt"]) != code
            if c["ignored"] != want_ignored:
                entries.append({"campaignId": c["campaignId"], "adAccountId": c["adAccountId"], "ignored": want_ignored})
                changes.append({"store": code, "campaign": c["name"], "action": "ignorerad" if want_ignored else "räknas"})
        if entries and not dry:
            after = {c["campaignId"]: c["ignored"] for c in juicy(tok, "/api/facebook/campaigns/ignore", {"entries": entries})["campaigns"]}
            bad = [e for e in entries if after.get(e["campaignId"]) != e["ignored"]]
            if bad:
                errs.append(f"{code}: kampanjkontrollen kunde inte ändra {len(bad)} kampanjer")
    if errs:
        raise PartialSync(changes, seen, errs)
    return changes, seen


class PartialSync(Exception):
    def __init__(self, changes, seen, errs):
        super().__init__("; ".join(errs))
        self.changes, self.seen = changes, seen


def riksbank(series, start, end):
    url = f"https://api.riksbank.se/swea/v1/Observations/{series}/{start}/{end}"
    for wait in (0, 10, 30, 60):  # Riksbankens öppna API begränsar antal anrop per minut
        time.sleep(wait)
        try:
            return sorted((o["date"], o["value"]) for o in http(url))
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
    raise RuntimeError(f"Riksbanken svarar inte ({series})")


def span_label(a, b):
    """'fre 13 nov' för en dag, '6–9 nov' / '28 okt–3 nov' för flera."""
    if a == b:
        return f"{DAYS_SV[a.weekday()]} {a.day} {MONTHS_SV[a.month-1]}"
    return f"{a.day}{'' if a.month == b.month else ' ' + MONTHS_SV[a.month-1]}–{b.day} {MONTHS_SV[b.month-1]}"


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    if "--env" in args:  # lokal körning: läs nycklarna ur .secrets-filen
        for line in open(args[args.index("--env") + 1], encoding="utf-8"):
            if line.startswith(("JUICY_", "META_")) and "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)
    tokens = {c: os.environ[f"JUICY_{c}_TOKEN"] for c, _ in STORES if os.environ.get(f"JUICY_{c}_TOKEN")}
    missing = [c for c, _ in STORES if c not in tokens]

    now = dt.datetime.now(ZoneInfo("Europe/Stockholm"))
    today = now.date()
    end = today - dt.timedelta(days=1)  # sista hela dagen
    # dagar från 1 jan (eller förra månadens början i januari) t.o.m. idag; sista index = idag (hittills)
    m_this = today.replace(day=1)
    m_prev_end = m_this - dt.timedelta(days=1)
    m_prev = m_prev_end.replace(day=1)
    jan1 = today.replace(month=1, day=1)
    first = min(m_prev, jan1, today - dt.timedelta(days=7))
    days = [first + dt.timedelta(days=i) for i in range((today - first).days + 1)]
    iso = [d.isoformat() for d in days]
    N = len(days)
    ix = lambda d: (d - first).days
    upto = lambda d: N - 2 if d < today else N - 1  # t.o.m. igår, eller bara idag den första
    periods = {"Idag": (N - 1, N - 1), "Igår": (N - 2, N - 2), "4 dagar": (N - 5, N - 2), "7 dagar": (N - 8, N - 2),
               "Denna månad": (ix(m_this), upto(m_this)), "Förra månaden": (ix(m_prev), ix(m_prev_end)),
               "I år": (ix(jan1), upto(jan1))}
    SHORT = 8  # annonser hämtas per dag för de sista 8 dagarna; längre perioder hämtas summerat
    LONG = ["Denna månad", "Förra månaden", "I år"]

    errors = []
    try:
        changes, seen = sync_campaigns(tokens, dry)
    except PartialSync as e:  # övriga butiker kontrollerade, bara de som felade saknas
        changes, seen = e.changes, e.seen
        errors.append(f"Kampanjkontroll: {e}")
    except Exception as e:
        changes, seen = [], {}
        errors.append(f"Kampanjkontroll: {e}")
    new_since = (end - dt.timedelta(days=2)).isoformat()
    new_camps = sorted(({"date": d, "name": n, "store": owner(n, d) or "ingen"} for n, d in seen.values() if d >= new_since),
                       key=lambda x: x["date"])

    # växelkurser: senaste noteringen på eller före varje dag (helger = fredagens kurs)
    rates = {"SEK": [1.0] * len(days)}
    start = (days[0] - dt.timedelta(days=10)).isoformat()
    for cur, series in (("DKK", "SEKDKKPMI"), ("NOK", "SEKNOKPMI"), ("EUR", "SEKEURPMI")):
        obs = riksbank(series, start, iso[-1])
        rates[cur] = [[v for d, v in obs if d <= day][-1] for day in iso]

    stores = {}
    for code, name in STORES:
        if code not in tokens:
            continue
        tok = tokens[code]
        try:
            q = urllib.parse.urlencode({"dateFrom": iso[0], "dateTo": iso[-1], "groupBy": "DAILY"})
            pl = juicy(tok, "/api/stats/shop/profitloss?" + q)
            cur = pl.get("currencyCode") or "SEK"
            r = rates[cur]
            byday = {g["dateFrom"]: g["data"] for g in pl["groups"]}
            row = lambda key: [round(byday.get(d, {}).get(key, 0) * r[i]) for i, d in enumerate(iso)]
            # annonser per dag (en hämtning per dag) -> topp 5 per period, kampanjer per dag och produkter
            off = N - SHORT
            daily = [juicy(tok, f"/api/stats/facebook/ads?dateFrom={d}&dateTo={d}")["data"] for d in iso[off:]]
            rs = r[off:]
            # längre perioder: en summerad hämtning per period, omräknad med periodens snittkurs
            longads = {}
            for per in LONG:
                a, b = periods[per]
                longads[per] = (juicy(tok, f"/api/stats/facebook/ads?dateFrom={iso[a]}&dateTo={iso[b]}")["data"],
                                sum(r[a:b + 1]) / (b - a + 1))
            top, top_src = {}, {}
            for per, (a, b) in periods.items():
                agg = {}
                src = [longads[per]] if per in LONG else [(daily[i - off], rs[i - off]) for i in range(a, b + 1)]
                for ads, k in src:
                    for x in ads:
                        t = agg.setdefault(x["name"], [x["name"], 0, 0, 0])
                        t[1] += x["adSpend"] * k; t[2] += x["netRevenueV2"] * k; t[3] += x["profitAfterAdSpend"] * k
                top[per] = [[n, round(sp), round(rv), round(pr)] for n, sp, rv, pr in sorted(agg.values(), key=lambda t: -t[1])[:5] if sp > 0]
            camps = {}  # sista 8 dagarna per dag (index 0 = dag N-8)
            for i, ads in enumerate(daily):
                for x in ads:
                    if not x["adSpend"] and not x["netRevenueV2"]:
                        continue
                    c = camps.setdefault(x.get("campaignName") or "?", {"S": [0.0] * SHORT, "R": [0.0] * SHORT, "P": [0.0] * SHORT})
                    c["S"][i] += x["adSpend"] * rs[i]; c["R"][i] += x["netRevenueV2"] * rs[i]; c["P"][i] += x["profitAfterAdSpend"] * rs[i]
            camps = {n: {k: [round(v) for v in vals] for k, vals in c.items()} for n, c in camps.items()}
            campsL = {}  # längre perioder: [spend, oms, vinst] per kampanj
            for per, (ads, k) in longads.items():
                cl = campsL[per] = {}
                for x in ads:
                    if not x["adSpend"] and not x["netRevenueV2"]:
                        continue
                    t = cl.setdefault(x.get("campaignName") or "?", [0.0, 0.0, 0.0])
                    t[0] += x["adSpend"] * k; t[1] += x["netRevenueV2"] * k; t[2] += x["profitAfterAdSpend"] * k
                campsL[per] = {n: [round(v) for v in t] for n, t in cl.items()}
            stores[code] = {"name": name, "cur": cur, "R": row("netRevenue"), "S": row("totalAdSpend"), "P": row("netProfit"),
                            "C": row("cogs"), "top": top, "camps": camps, "campsL": campsL}
            if code in PRODUCT_STORES:  # per produkt och land (Juicys vinst efter annonskostnad per annons)
                prods = {}
                for i, ads in enumerate(daily):
                    for x in ads:
                        if not x["adSpend"] and not x["netRevenueV2"]:
                            continue
                        prod, land = product_of(x.get("campaignName"))
                        pr = prods.setdefault(prod, {}).setdefault(land, {"R": [0] * SHORT, "S": [0] * SHORT, "P": [0] * SHORT})
                        pr["R"][i] += x["netRevenueV2"] * rs[i]; pr["S"][i] += x["adSpend"] * rs[i]; pr["P"][i] += x["profitAfterAdSpend"] * rs[i]
                stores[code]["products"] = {p: {l: {k: [round(v) for v in vals] for k, vals in d.items()} for l, d in ls.items()}
                                            for p, ls in prods.items()}
                prodsL = {}  # längre perioder: {produkt: {land: [oms, spend, vinst]}}
                for per, (ads, k) in longads.items():
                    pl_ = prodsL[per] = {}
                    for x in ads:
                        if not x["adSpend"] and not x["netRevenueV2"]:
                            continue
                        prod, land = product_of(x.get("campaignName"))
                        t = pl_.setdefault(prod, {}).setdefault(land, [0.0, 0.0, 0.0])
                        t[0] += x["netRevenueV2"] * k; t[1] += x["adSpend"] * k; t[2] += x["profitAfterAdSpend"] * k
                    prodsL[per] = {p: {l: [round(v) for v in t] for l, t in ls.items()} for p, ls in pl_.items()}
                stores[code]["productsL"] = prodsL
        except Exception as e:
            errors.append(f"{name}: {e}")

    surf_data = None
    os.makedirs(os.path.join(HERE, "state"), exist_ok=True)  # cache-steget i workflowen kräver mappen
    if os.environ.get("META_BUDGET_TOKEN"):
        try:
            sys.path.insert(0, HERE)
            import surf
            surf_stores = {c: {"token": tokens[c], "rate": rates[stores[c]["cur"]][-1] if c in stores else 1}
                           for c in surf.SURF_STORES if c in tokens}
            surf_data = surf.run(juicy, surf_stores, os.environ["META_BUDGET_TOKEN"].strip(), owner, now,
                                 live=os.environ.get("SURF_LIVE", "").strip().lower() == "true")
            errors += [f"Surf-scaling: {x}" for x in surf_data.get("errors", [])]
        except Exception as e:
            errors.append(f"Surf-scaling: {e}")

    data = {
        "surf": surf_data,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "labels": [f"{DAYS_SV[d.weekday()]} {d.day}" for d in days],
        "mon": [d.month - 1 for d in days],
        "short": SHORT,
        "periods": {p: {"from": a, "to": b, "long": p in LONG, "month": p == "I år",
                        "label": span_label(days[a], days[b]) + (" (hittills)" if p == "Idag" or b == N - 1 else "")}
                    for p, (a, b) in periods.items()},
        "order": [c for c, _ in STORES if c in stores],
        "stores": stores,
        "sync": {"changes": changes, "new": new_camps},
        "missing": [n for c, n in STORES if c in missing],
        "errors": errors,
    }
    html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    html = html.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
    open(os.path.join(HERE, "site", "index.html"), "w", encoding="utf-8").write(html)

    print(f"Rapport {iso[0]}–{iso[-1]}: {len(stores)} butiker, {len(changes)} kampanjändringar, {len(new_camps)} nya kampanjer")
    for c in changes:
        print("  ", c["store"], c["action"], c["campaign"])
    for e in errors:
        print("FEL:", e)
    if errors:
        open(os.path.join(HERE, "errors.txt"), "w").write("\n".join(errors))


if __name__ == "__main__":
    main()
