"""Surf-scaling för Homelyst, TESTLÄGE: räknar ut vad som skulle göras men ändrar ingenting i Meta.

Regler (Johns, 13 nov 2026):
- Kontrollpunkter kl 07, 10, 13, 16, 19 (svensk tid). Kl 07 bedöms gårdagens ROAS, övriga dagens ROAS sedan midnatt.
- Höj budgeten +50 % om ROAS (Juicy) >= 1,3 × break-even-ROAS och spend >= MIN_SPEND.
- CBO: kampanjbudgeten. ABO: varje annonsuppsättnings budget.
- Kl 23:30 återgår allt till budgeten som gällde innan dagens första höjning.
- Alla aktiva kampanjer med HOMELYST i namnet.

State (originalbudget, antal höjningar, logg) sparas i state/surf.json mellan körningarna
(GitHub Actions-cache, aldrig publikt). Det här skriptet anropar bara Metas läs-API.
"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v21.0"
AD_ACCOUNT = "act_3037939703006283"
NAME_MATCH = "HOMELYST"
CHECKPOINTS = [7, 10, 13, 16, 19]
RESET_AT = (23, 30)
STEP = 1.5
MAX_STEPS = 5
MIN_SPEND = 300  # kr spend i bedömningsperioden innan ett beslut tas
MARGIN = 1.3  # gräns = MARGIN × break-even-ROAS
COST_KEYS = ["cogs", "transactionFees", "shippingCosts", "thirdPartyLogisticsCosts", "orderCosts",
             "returnCosts", "euFees", "euRegulationFee"]
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state", "surf.json")


def meta_get(path, token, **params):
    params["access_token"] = token
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    return json.load(urllib.request.urlopen(url, timeout=60))


def entities(token):
    """Aktiva Homelyst-kampanjer: CBO → kampanjen, ABO → varje aktiv annonsuppsättning."""
    d = meta_get(f"{AD_ACCOUNT}/campaigns", token, limit=200,
                 fields="name,effective_status,daily_budget,adsets.limit(100){name,effective_status,daily_budget}",
                 filtering=json.dumps([{"field": "name", "operator": "CONTAIN", "value": NAME_MATCH}]))
    out = []
    for c in d.get("data", []):
        if c["effective_status"] != "ACTIVE":
            continue
        if c.get("daily_budget"):
            out.append({"id": c["id"], "level": "kampanj (CBO)", "name": c["name"], "campaign": c["name"],
                        "budget": int(c["daily_budget"]) / 100, "match": ("campaignId", c["id"])})
        else:
            for a in c.get("adsets", {}).get("data", []):
                if a["effective_status"] == "ACTIVE" and a.get("daily_budget"):
                    out.append({"id": a["id"], "level": "annonsuppsättning (ABO)", "name": a["name"], "campaign": c["name"],
                                "budget": int(a["daily_budget"]) / 100, "match": ("adSetId", a["id"])})
    return out


def run(juicy, juicy_token, meta_token, now):
    """juicy(token, path) -> json. Returnerar data för rapporten."""
    today = now.date().isoformat()
    yday = (now.date() - dt.timedelta(days=1)).isoformat()
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    if state.get("date") != today:
        state = {"date": today, "done": [], "reset": False, "ent": {}, "log": []}

    # break-even-ROAS från Juicy, senaste 7 hela dagarna
    q = urllib.parse.urlencode({"dateFrom": (now.date() - dt.timedelta(days=7)).isoformat(), "dateTo": yday, "groupBy": "DAILY"})
    s = juicy(juicy_token, "/api/stats/shop/profitloss?" + q)["summary"]
    costs = sum(s.get(k) or 0 for k in COST_KEYS) - (s.get("restockedCogs") or 0)
    be = s["netRevenue"] / (s["netRevenue"] - costs) if s["netRevenue"] > costs else None
    limit = round(be * MARGIN, 2) if be else None

    ents = entities(meta_token)
    for e in ents:  # originalbudget = budgeten första gången vi ser enheten i dag
        st = state["ent"].setdefault(e["id"], {"base": e["budget"], "steps": 0, "name": e["name"], "level": e["level"]})
        st["now_meta"] = e["budget"]

    def perf(day_from, day_to):
        ads = juicy(juicy_token, f"/api/stats/facebook/ads?dateFrom={day_from}&dateTo={day_to}")["data"]
        res = {}
        for e in ents:
            k, v = e["match"]
            sp = sum(a["adSpend"] for a in ads if a.get(k) == v)
            rv = sum(a["netRevenueV2"] for a in ads if a.get(k) == v)
            res[e["id"]] = (sp, rv)
        return res

    due = [c for c in CHECKPOINTS if now.hour >= c and c not in state["done"]]
    if due and limit:
        cp = max(due)
        state["done"] += due
        p = perf(yday, yday) if cp == CHECKPOINTS[0] else perf(today, today)
        for e in ents:
            st = state["ent"][e["id"]]
            sp, rv = p.get(e["id"], (0, 0))
            roas = rv / sp if sp else 0
            if sp < MIN_SPEND:
                act, why = "håll", f"för lite data ({round(sp)} kr spend)"
            elif roas < limit:
                act, why = "håll", f"ROAS {roas:.2f} under gränsen {limit:.2f}"
            elif st["steps"] >= MAX_STEPS:
                act, why = "håll", f"redan {MAX_STEPS} höjningar i dag"
            else:
                act, why = "höj +50 %", f"ROAS {roas:.2f} ≥ {limit:.2f}"
                st["steps"] += 1
            st["sim"] = round(st["base"] * STEP ** st["steps"])
            state["log"].append({"t": f"{cp:02d}:00", "id": e["id"], "name": e["name"], "act": act, "why": why,
                                 "spend": round(sp), "roas": round(roas, 2), "budget": st["sim"],
                                 "basis": "gårdagen" if cp == CHECKPOINTS[0] else "i dag"})

    if (now.hour, now.minute) >= RESET_AT and not state["reset"]:
        state["reset"] = True
        for i, st in state["ent"].items():
            if st["steps"]:
                state["log"].append({"t": "23:30", "id": i, "name": st["name"], "act": "återställ",
                                     "why": f"tillbaka till {round(st['base'])} kr", "budget": round(st["base"])})
            st["steps"], st["sim"] = 0, round(st["base"])

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    nxt = next((f"{c:02d}:00" for c in CHECKPOINTS if c not in state["done"]), "23:30 (återställning)" if not state["reset"] else "i morgon 07:00")
    return {
        "test": True, "be": round(be, 2) if be else None, "limit": limit, "margin": MARGIN, "minSpend": MIN_SPEND,
        "next": nxt, "log": state["log"],
        "ent": [{"name": st["name"], "level": st["level"], "base": round(st["base"]), "steps": st["steps"],
                 "sim": st.get("sim", round(st["base"])), "meta": round(st.get("now_meta", st["base"]))}
                for st in state["ent"].values()],
    }
