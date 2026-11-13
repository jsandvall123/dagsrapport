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
    changes, seen = [], {}
    for code, tok in tokens.items():
        camps = juicy(tok, "/api/facebook/credentials")["campaigns"]
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
                raise RuntimeError(f"{code}: kampanjkontrollen kunde inte ändra {len(bad)} kampanjer")
    return changes, seen


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
    days = [today - dt.timedelta(days=7 - i) for i in range(8)]  # index 0–6 = 7 hela dagar, 7 = idag (hittills)
    iso = [d.isoformat() for d in days]
    periods = {"Idag": (7, 7), "Igår": (6, 6), "4 dagar": (3, 6), "7 dagar": (0, 6)}

    errors = []
    try:
        changes, seen = sync_campaigns(tokens, dry)
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
            top = {}
            for per, (a, b) in periods.items():
                ads = juicy(tok, f"/api/stats/facebook/ads?dateFrom={iso[a]}&dateTo={iso[b]}")["data"]
                avg = sum(r[a:b + 1]) / (b + 1 - a)
                top[per] = [[x["name"], round(x["adSpend"] * avg), round(x["netRevenueV2"] * avg), round(x["profitAfterAdSpend"] * avg)]
                            for x in sorted(ads, key=lambda x: -x["adSpend"])[:5] if x["adSpend"] > 0]
            stores[code] = {"name": name, "cur": cur, "R": row("netRevenue"), "S": row("totalAdSpend"), "P": row("netProfit"), "top": top}
        except Exception as e:
            errors.append(f"{name}: {e}")

    surf_data = None
    os.makedirs(os.path.join(HERE, "state"), exist_ok=True)  # cache-steget i workflowen kräver mappen
    if os.environ.get("META_BUDGET_TOKEN") and "HOMELYST" in tokens:
        try:
            sys.path.insert(0, HERE)
            import surf
            surf_data = surf.run(juicy, tokens["HOMELYST"], os.environ["META_BUDGET_TOKEN"].strip(), now)
        except Exception as e:
            errors.append(f"Surf-scaling (test): {e}")

    data = {
        "surf": surf_data,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "labels": [f"{DAYS_SV[d.weekday()]} {d.day}" for d in days],
        "periods": {p: {"from": a, "to": b, "label": span_label(days[a], days[b]) + (" (hittills)" if p == "Idag" else "")}
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
