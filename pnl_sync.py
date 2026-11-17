"""Fyller i PNL-sheetsen (en per butik) med Juicys siffror.

Varje natt skrivs de senaste DAYS_BACK stängda dagarna (t.o.m. igår), så att sena returer och
Metas efterjusteringar av annonskostnaden kommer med. Bara dessa kolumner skrivs:
  B REV      = netRevenue + returns  (försäljning före returer)
  C COGS     = cogs - restockedCogs
  D FEES     = transactionFees
  E ADSPEND  = totalAdSpend
  J REFUNDS  = returns               (dras av från vinsten i månadens totalrad)
Allt annat (A, F–H formler, I OPEX, K TEAM, L CREATORS) lämnas orört. Belopp i butikens valuta.
Månadsvinsten i sheeten = B − C − D − E − J = Juicys netProfit.

Layout (samma i alla sheets): en flik per månad, "November 2026", rad 2 = dag 1.

Nyckel: GOOGLE_SA_JSON (tjänstkonto med redigeringsrätt till sheetsen).
Lokalt utan nyckel: python3 pnl_sync.py --env ../.secrets/shopify-stores.env --print [--from 2026-11-01 --to 2026-11-16]
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
DAYS_BACK = 7
SHEETS = {  # butikskod -> Google Sheet-id
    "SE": "1IAK5K8gZtPfA4zBUp86N3viyvMnWOlbqlLc2okgLh5E",
    "DK": "1A2RggeMqv9zlsWqcBeUgp5lsn2o7hnOi0hph49Jm4HQ",
    "NO": "1-LwydBEcZnhjxNPe71JQ1W8Tjn15QeloWYXUyCjP5os",
    "FI": "1_m0kBk-igPsn7ZKgU33yNF7HyxTefh3e0IqQPPJ_34g",
    "DE": "1DWq4nt22qjZ0dLnWqs32IBeSSFSymkLdGMXZ0q-DDpc",
    "HOMELYST": "143sMzy5SEUfo4x9f7r7jkYG4JT2hCKIpYG-t7QagXgE",
    "HUSTOVALD": "1_l7Kscoo9zvxW9oo3EMLDvKJU0a_Hu7kFbA0nlZRuG4",
    "JUNO": "1Srs6Iomec2CuihtAQE8NWzEEe2dl-vTuYS1zNhznFK0",
    "EARLO": "1AemHHHhTUAdO5J6uuCmHyCG9Jwk4X4TQqn_tdcTZ3Sg",
}
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]


def juicy_days(token, d0, d1):
    q = urllib.parse.urlencode({"dateFrom": d0.isoformat(), "dateTo": d1.isoformat(), "groupBy": "DAILY"})
    req = urllib.request.Request(f"{JUICY}/api/stats/shop/profitloss?{q}",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    for wait in (0, 10, 30):  # Juicy svarar ibland långsamt
        time.sleep(wait)
        try:
            groups = json.load(urllib.request.urlopen(req, timeout=90))["groups"]
            break
        except Exception:
            if wait == 30:
                raise
    out = {}
    for g in groups:
        d = g["data"]
        ret = d.get("returns") or 0
        out[g["dateFrom"][:10]] = {
            "B": round((d.get("netRevenue") or 0) + ret, 2),
            "C": round((d.get("cogs") or 0) - (d.get("restockedCogs") or 0), 2),
            "D": round(d.get("transactionFees") or 0, 2),
            "E": round(d.get("totalAdSpend") or 0, 2),
            "J": round(ret, 2),
        }
    return out


def ranges_for(days):
    """{datum: värden} -> lista med {range, values} för Sheets values.batchUpdate."""
    data = []
    for day, v in sorted(days.items()):
        d = dt.date.fromisoformat(day)
        tab = f"'{MONTHS_EN[d.month - 1]} {d.year}'"
        row = d.day + 1
        data.append({"range": f"{tab}!B{row}:E{row}", "values": [[v["B"], v["C"], v["D"], v["E"]]]})
        # J bara när det finns returer, eller när cellen redan fyllts av oss tidigare (0 skriver över gammalt)
        data.append({"range": f"{tab}!J{row}", "values": [[v["J"] if v["J"] else ""]]})
    return data


def google_token(sa):
    from google.oauth2 import service_account  # pip install google-auth
    import google.auth.transport.requests
    cred = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    cred.refresh(google.auth.transport.requests.Request())
    return cred.token


def write(token, sheet_id, data):
    body = json.dumps({"valueInputOption": "RAW", "data": data}).encode()
    req = urllib.request.Request(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate",
                                 data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    for wait in (0, 20, 60):  # Sheets begränsar antal skrivningar per minut
        time.sleep(wait)
        try:
            return json.load(urllib.request.urlopen(req, timeout=90))
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise RuntimeError(f"Sheets {e.code}: {e.read()[:300].decode(errors='replace')}")
    raise RuntimeError("Sheets svarar 429 för ofta")


def run(tokens, now, d0=None, d1=None, dry=False):
    """Returnerar (antal skrivna dagar, fel). Kräver GOOGLE_SA_JSON om inte dry."""
    d1 = d1 or now.date() - dt.timedelta(days=1)
    d0 = d0 or d1 - dt.timedelta(days=DAYS_BACK - 1)
    errors, plan = [], {}
    for code, sheet_id in SHEETS.items():
        if code not in tokens:
            errors.append(f"PNL {code}: Juicy-nyckel saknas")
            continue
        try:
            plan[code] = ranges_for(juicy_days(tokens[code], d0, d1))
        except Exception as e:
            errors.append(f"PNL {code}: Juicy: {e}")
    if dry:
        return plan, errors
    sa = json.loads(os.environ["GOOGLE_SA_JSON"])
    gtok = google_token(sa)
    for code, data in plan.items():
        try:
            write(gtok, SHEETS[code], data)
        except Exception as e:
            errors.append(f"PNL {code}: {e}")
    return plan, errors


def nightly():
    """Körs av workflowen var 15:e minut men skriver bara en gång per natt (från kl 04, när dagen är stängd)."""
    here = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(here, "state", "pnl.txt")
    now = dt.datetime.now(ZoneInfo("Europe/Stockholm"))
    today = now.date().isoformat()
    if not os.environ.get("GOOGLE_SA_JSON", "").strip():
        print("PNL: GOOGLE_SA_JSON saknas, hoppar över")
        return
    if now.hour < 4 or (os.path.exists(marker) and open(marker).read().strip() == today):
        print("PNL: redan gjort i natt eller före 04")
        return
    toks = {c: os.environ[f"JUICY_{c}_TOKEN"] for c in SHEETS if os.environ.get(f"JUICY_{c}_TOKEN")}
    plan, errs = run(toks, now)
    print(f"PNL: {len(plan)} sheets uppdaterade ({DAYS_BACK} dagar t.o.m. igår)")
    if errs:
        with open(os.path.join(here, "errors.txt"), "a") as f:
            f.write("\n" + "\n".join(errs))
        return
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    open(marker, "w").write(today)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--nightly" in args:
        nightly()
        sys.exit(0)
    if "--env" in args:
        for line in open(args[args.index("--env") + 1], encoding="utf-8"):
            if line.startswith("JUICY_") and "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)
    toks = {c: os.environ[f"JUICY_{c}_TOKEN"] for c in SHEETS if os.environ.get(f"JUICY_{c}_TOKEN")}
    get = lambda f: dt.date.fromisoformat(args[args.index(f) + 1]) if f in args else None
    plan, errs = run(toks, dt.datetime.now(ZoneInfo("Europe/Stockholm")), get("--from"), get("--to"), dry="--print" in args)
    if "--print" in args:
        print(json.dumps(plan, ensure_ascii=False))
    for e in errs:
        print("FEL:", e, file=sys.stderr)
    sys.exit(1 if errs else 0)
