"""Surf-scaling för Homelyst (sedan 13 nov 2026) och Isella SE/DK/NO/FI/DE (sedan 15 nov 2026).

Regler (John, 13 nov 2026, samma för alla butiker):
- Kl 07 sparas en ögonblicksbild av dagens spend/omsättning per kampanj. Ingen höjning.
- Kontrollpunkter kl 09, 12, 15, 18, 21: ROAS mäts från kl 07 till nu (Juicy).
- Höj +50 % (+25 % när budgeten redan är minst 25 000 kr) om ROAS >= 1,3 × butikens egen break-even-ROAS
  och spend sedan 07 >= MIN_SPEND. Aldrig över 4 × dagens originalbudget.
- Tillägg 15 nov 2026: höjning kräver också att ROAS sedan förra kontrollen är över gränsen (om fönstret
  har minst MIN_SPEND i spend). Är ROAS sedan förra kontrollen under break-even sänks budgeten −25 %,
  aldrig under originalbudgeten.
- CBO: kampanjbudgeten. ABO: varje annonsuppsättnings budget. Alla aktiva kampanjer som owner() ger en butik i SURF_STORES.
- Ändrar John budgeten själv under dagen fortsätter skriptet från hans nya budget.
- Kl 23:30 återställs allt till budgeten som gällde när dagen började (originalbudgeten).

LIVE styrs av miljövariabeln SURF_LIVE ("true" = skriv budgetar i Meta). Annars testläge: bara beräkning.
State sparas i state/surf.json mellan körningarna (GitHub Actions-cache, aldrig publikt).
"""
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v21.0"
AD_ACCOUNT = "act_3037939703006283"
# Homelyst borttagen 17 nov 2026 (Johns beslut): 80–90k omsättning med ~10 % vinst, budgetar sätts manuellt.
SURF_STORES = ["SE", "DK", "NO", "FI", "DE"]  # butikskoder från owner() i build.py
SNAPSHOT_AT = 7
CHECKPOINTS = [9, 12, 15, 18, 21]
RESET_AT = (23, 30)
STEP = 1.5
STEP_HIGH = 1.25  # försiktigare höjning när budgeten nått HIGH_FROM
HIGH_FROM = 25000  # kr/dag
STEP_DOWN = 0.75  # sänkning när senaste fönstret går under break-even
MAX_MULT = 4  # aldrig över 4 × originalbudgeten
# Lägre tak för enskilda kampanjer (del av kampanjnamnet → multipel). Johns beslut 15 nov 2026:
# DK-äggkokaren bär nästan hela spenden på två annonser; 3x tills de nya annonserna (hl-dk_C03) är live.
MAX_MULT_FOR = {"DK//HOMELYST//MICROWAVE EGG POACHER": 3}


def max_mult(name):
    return min([m for k, m in MAX_MULT_FOR.items() if k in (name or "")] + [MAX_MULT])
MIN_SPEND = 300  # kr spend sedan 07 innan ett beslut tas
MARGIN = 1.3  # gräns = MARGIN × break-even-ROAS
COST_KEYS = ["cogs", "transactionFees", "shippingCosts", "thirdPartyLogisticsCosts", "orderCosts",
             "returnCosts", "euFees", "euRegulationFee"]
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state", "surf.json")


def meta(path, token, data=None, **params):
    params["access_token"] = token
    if data is None:
        req = urllib.request.Request(f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}")
    else:
        body = urllib.parse.urlencode({**data, "access_token": token}).encode()
        req = urllib.request.Request(f"{GRAPH}/{path}", data=body, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Meta {e.code}: {e.read()[:300].decode(errors='replace')}")


def set_budget(token, obj_id, kr):
    r = meta(obj_id, token, data={"daily_budget": str(int(round(kr * 100)))})
    if not r.get("success"):
        raise RuntimeError(f"Meta svarade {r}")


def running(o):
    """Påslagen och levererar. IN_PROCESS = Meta bearbetar en nyss gjord ändring (t.ex. vår budgethöjning)."""
    return o.get("status") == "ACTIVE" and o.get("effective_status") in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES")


def entities(token, owner):
    """Aktiva kampanjer i surf-butikerna: CBO → kampanjen, ABO → varje aktiv annonsuppsättning.
    owner(namn, skapad) -> butikskod (samma regel som kampanjkontrollen i build.py)."""
    d = meta(f"{AD_ACCOUNT}/campaigns", token, limit=300,
             fields="name,status,effective_status,daily_budget,created_time,adsets.limit(100){name,status,effective_status,daily_budget}",
             filtering=json.dumps([{"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "IN_PROCESS", "WITH_ISSUES"]}]))
    out = []
    for c in d.get("data", []):
        code = owner(c["name"], c.get("created_time", "")[:10])
        if code not in SURF_STORES or not running(c):
            continue
        if c.get("daily_budget"):
            out.append({"id": c["id"], "store": code, "level": "kampanj (CBO)", "name": c["name"],
                        "budget": int(c["daily_budget"]) / 100, "match": ("campaignId", c["id"]), "camp": c["name"]})
        else:
            for a in c.get("adsets", {}).get("data", []):
                if running(a) and a.get("daily_budget"):
                    out.append({"id": a["id"], "store": code, "level": "annonsuppsättning (ABO)", "name": a["name"],
                                "budget": int(a["daily_budget"]) / 100, "match": ("adSetId", a["id"]), "camp": c["name"]})
    return out


def break_even(juicy, token, now):
    """Break-even-ROAS från Juicy, senaste 7 hela dagarna (butikens egna marginaler)."""
    q = urllib.parse.urlencode({"dateFrom": (now.date() - dt.timedelta(days=7)).isoformat(),
                                "dateTo": (now.date() - dt.timedelta(days=1)).isoformat(), "groupBy": "DAILY"})
    s = juicy(token, "/api/stats/shop/profitloss?" + q)["summary"]
    costs = sum(s.get(k) or 0 for k in COST_KEYS) - (s.get("restockedCogs") or 0)
    return s["netRevenue"] / (s["netRevenue"] - costs) if s["netRevenue"] > costs else None


def run(juicy, stores, meta_token, owner, now, live=False):
    """juicy(token, path) -> json. stores = {kod: {"token": juicy-token, "rate": lokal valuta → SEK}}.
    Budgetar i Meta är i SEK (annonskontots valuta). ROAS räknas i butikens egen valuta i Juicy,
    spend-golvet räknas om till SEK. Returnerar data för rapporten; fel samlas i 'errors'."""
    today = now.date().isoformat()
    hhmm = now.strftime("%H:%M")
    errors = []
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    if state.get("date") == today and state.get("v") == 2:  # gårdagens format (bara Homelyst) → v3
        for st in state["ent"].values():
            st["store"] = "HOMELYST"
        state = {"v": 3, "date": today, "done": state["done"], "reset": state["reset"], "ent": state["ent"],
                 "snap": {"HOMELYST": state["snap"]} if state["snap"] else {},
                 "log": [{**l, "store": "HOMELYST"} for l in state["log"]]}
    if state.get("date") != today or state.get("v") != 3:
        state = {"v": 3, "date": today, "snap": {}, "done": [], "reset": False, "ent": {}, "log": []}

    def log(t, store, e_id, name, act, why, **kw):
        state["log"].append({"t": t, "store": store, "id": e_id, "name": name, "act": act, "why": why, **kw})

    info = {}
    for code, s in stores.items():
        try:
            be = break_even(juicy, s["token"], now)
        except Exception as ex:
            errors.append(f"{code}: break-even kunde inte hämtas: {ex}")
            be = None
        info[code] = {"be": round(be, 2) if be else None, "limit": round(be * MARGIN, 2) if be else None}

    ents = [e for e in entities(meta_token, owner) if e["store"] in stores]
    for e in ents:
        st = state["ent"].get(e["id"])
        if st is None:  # första gången i dag: originalbudget = nuvarande budget
            st = state["ent"][e["id"]] = {"base": e["budget"], "cur": e["budget"], "seen": e["budget"], "steps": 0,
                                          "name": e["name"], "level": e["level"], "store": e["store"]}
        elif abs(e["budget"] - st["seen"]) >= 1:  # budgeten ändrad i Meta av någon annan än skriptet
            log(hhmm, e["store"], e["id"], e["name"], "manuell ändring",
                f"budgeten ändrad från {round(st['seen'])} till {round(e['budget'])} kr i Meta, fortsätter därifrån")
            st["cur"] = st["seen"] = e["budget"]
        st["meta"] = e["budget"]
        st["camp"] = e["camp"]

    ads_cache = {}

    def cumulative(code):
        if code not in ads_cache:
            ads_cache[code] = juicy(stores[code]["token"], f"/api/stats/facebook/ads?dateFrom={today}&dateTo={today}")["data"]
        ads = ads_cache[code]
        res = {}
        for e in ents:
            if e["store"] != code:
                continue
            k, v = e["match"]
            res[e["id"]] = [sum(a["adSpend"] for a in ads if a.get(k) == v),
                            sum(a["netRevenueV2"] for a in ads if a.get(k) == v)]
        return res

    # ögonblicksbild kl 07, per butik. Startar en butik sent mäts den från då, och kontroller
    # närmare än en timme efter starten hoppas över (inga beslut på ett för kort fönster).
    if now.hour >= SNAPSHOT_AT:
        for code in stores:
            if code in state["snap"]:
                continue
            try:
                state["snap"][code] = {"t": hhmm, "v": cumulative(code)}
            except Exception as ex:
                errors.append(f"{code}: ögonblicksbild misslyckades: {ex}")
                continue
            late = now.hour > SNAPSHOT_AT or now.minute > 30
            skipped = [c for c in CHECKPOINTS if late and c * 60 < now.hour * 60 + now.minute + 60]
            state["snap"][code]["skip"] = skipped
            log(hhmm, code, "", "Alla kampanjer", "mätning startar",
                f"ROAS mäts från {hhmm}" + (f", kontroll {', '.join(f'{c:02d}' for c in skipped)} hoppas över" if skipped else ""))

    due = [c for c in CHECKPOINTS if now.hour >= c and c not in state["done"]]
    if due and not state["reset"]:
        cp = max(due)
        state["done"] += due
        for code in stores:
            snapd = state["snap"].get(code)
            lim = info[code]["limit"]
            if not snapd or cp in snapd.get("skip", []) or not lim:
                continue
            try:
                cum = cumulative(code)
            except Exception as ex:
                errors.append(f"{code}: Juicy svarade inte vid kontrollen {cp:02d}: {ex}")
                continue
            rate = stores[code].get("rate", 1)
            be = info[code]["be"]
            for e in ents:
                if e["store"] != code:
                    continue
                st = state["ent"][e["id"]]
                s0 = snapd["v"].get(e["id"], [0, 0])
                c1 = cum[e["id"]]
                sp, rv = c1[0] - s0[0], c1[1] - s0[1]
                # fönstret sedan förra kontrollen (eller sedan mätningen startade)
                w0 = st.get("last") or s0
                wsp, wrv = c1[0] - w0[0], c1[1] - w0[1]
                wsince = st.get("last_t") or snapd["t"]
                st["last"], st["last_t"] = c1, f"{cp:02d}:00"
                sp_sek, wsp_sek = sp * rate, wsp * rate
                roas = rv / sp if sp > 0 else 0
                wroas = wrv / wsp if wsp > 0 else 0
                mult = max_mult(st["camp"] if st.get("camp") else st["name"])
                cap = st["base"] * mult
                base = dict(spend=round(sp_sek), roas=round(roas, 2), wspend=round(wsp_sek), wroas=round(wroas, 2))
                t = f"{cp:02d}:00"
                since = snapd["t"]
                wtxt = f"sedan {wsince}: ROAS {wroas:.2f} på {round(wsp_sek)} kr"

                # 1. sänk: senaste fönstret under break-even → −25 %, aldrig under originalbudgeten
                if be and wsp_sek >= MIN_SPEND and wroas < be and st["cur"] > st["base"] + 1:
                    new = max(round(st["cur"] * STEP_DOWN), round(st["base"]))
                    pct = round((1 - new / st["cur"]) * 100)
                    if live:
                        try:
                            set_budget(meta_token, e["id"], new)
                        except Exception as ex:
                            errors.append(f"{e['name']}: sänkning misslyckades: {ex}")
                            log(t, code, e["id"], e["name"], "fel", f"kunde inte sänka: {ex}", **base)
                            continue
                    log(t, code, e["id"], e["name"], f"sänk −{pct} %",
                        f"{wtxt}, under break-even {be:.2f}; {round(st['cur'])} → {new} kr", budget=new, **base)
                    st["cur"], st["downs"] = new, st.get("downs", 0) + 1
                    if live:
                        st["seen"] = new
                    continue

                # 2. höj: ROAS sedan 07 OCH senaste fönstret över gränsen
                if sp_sek < MIN_SPEND:
                    log(t, code, e["id"], e["name"], "håll", f"för lite data ({round(sp_sek)} kr spend sedan {since})", **base)
                    continue
                if roas < lim:
                    log(t, code, e["id"], e["name"], "håll", f"ROAS {roas:.2f} sedan {since} under gränsen {lim:.2f}", **base)
                    continue
                if wsp_sek >= MIN_SPEND and wroas < lim:  # för lite spend i fönstret → beslut på hela dagen
                    log(t, code, e["id"], e["name"], "håll",
                        f"ROAS {roas:.2f} sedan {since} men {wtxt}, under gränsen {lim:.2f}", **base)
                    continue
                if st["cur"] >= cap - 1:
                    log(t, code, e["id"], e["name"], "håll", f"maxtaket {round(cap)} kr ({mult} × original) nått", **base)
                    continue
                f = STEP_HIGH if st["cur"] >= HIGH_FROM else STEP
                new = min(round(st["cur"] * f), round(cap))
                pct = round((new / st["cur"] - 1) * 100)
                if live:
                    try:
                        set_budget(meta_token, e["id"], new)
                    except Exception as ex:
                        errors.append(f"{e['name']}: höjning misslyckades: {ex}")
                        log(t, code, e["id"], e["name"], "fel", f"kunde inte höja: {ex}", **base)
                        continue
                log(t, code, e["id"], e["name"], f"höj +{pct} %",
                    f"ROAS {roas:.2f} sedan {since} och {wroas:.2f} sedan {wsince} ≥ {lim:.2f}, {round(st['cur'])} → {new} kr",
                    budget=new, **base)
                st["cur"], st["steps"] = new, st["steps"] + 1
                if live:
                    st["seen"] = new

    # återställning 23:30 (görs om vid nästa körning om något misslyckas)
    if (now.hour, now.minute) >= RESET_AT and not state["reset"]:
        ok = True
        for i, st in state["ent"].items():
            if abs(st["cur"] - st["base"]) < 1:
                continue
            if live:
                try:
                    set_budget(meta_token, i, st["base"])
                except Exception as ex:
                    ok = False
                    errors.append(f"{st['name']}: återställning misslyckades: {ex}")
                    continue
            log(hhmm, st["store"], i, st["name"], "återställ", f"{round(st['cur'])} → {round(st['base'])} kr (original)",
                budget=round(st["base"]))
            st["cur"] = st["base"]
            if live:
                st["seen"] = st["base"]
        state["reset"] = ok

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    if now.hour < SNAPSHOT_AT:
        nxt = f"{SNAPSHOT_AT:02d}:00 (mätning startar)"
    else:
        nxt = next((f"{c:02d}:00" for c in CHECKPOINTS if c not in state["done"]),
                   "23:30 (återställning)" if not state["reset"] else "i morgon 07:00")
    for code in stores:
        info[code]["snap"] = (state["snap"].get(code) or {}).get("t")
    return {
        "test": not live, "highFrom": HIGH_FROM, "maxMult": MAX_MULT, "margin": MARGIN, "minSpend": MIN_SPEND,
        "next": nxt, "log": state["log"], "errors": errors, "stores": info,
        "ent": [{"store": st["store"], "name": st["name"], "level": st["level"], "base": round(st["base"]), "steps": st["steps"], "downs": st.get("downs", 0), "mult": max_mult(st.get("camp") or st["name"]),
                 "sim": round(st["cur"]), "meta": round(st.get("meta", st["cur"]))} for st in state["ent"].values()],
    }
