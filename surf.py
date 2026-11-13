"""Surf-scaling för Homelyst.

Regler (John, 13 nov 2026):
- Kl 07 sparas en ögonblicksbild av dagens spend/omsättning per kampanj. Ingen höjning.
- Kontrollpunkter kl 09, 12, 15, 18, 21: ROAS mäts från kl 07 till nu (Juicy).
- Höj +50 % (+25 % när budgeten redan är minst 25 000 kr) om ROAS >= 1,3 × break-even-ROAS
  och spend sedan 07 >= MIN_SPEND. Aldrig över 4 × dagens originalbudget.
- CBO: kampanjbudgeten. ABO: varje annonsuppsättnings budget. Alla aktiva kampanjer med HOMELYST i namnet.
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
NAME_MATCH = "HOMELYST"
SNAPSHOT_AT = 7
CHECKPOINTS = [9, 12, 15, 18, 21]
RESET_AT = (23, 30)
STEP = 1.5
STEP_HIGH = 1.25  # försiktigare höjning när budgeten nått HIGH_FROM
HIGH_FROM = 25000  # kr/dag
MAX_MULT = 4  # aldrig över 4 × originalbudgeten
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


def entities(token):
    """Aktiva Homelyst-kampanjer: CBO → kampanjen, ABO → varje aktiv annonsuppsättning."""
    d = meta(f"{AD_ACCOUNT}/campaigns", token, limit=200,
             fields="name,status,effective_status,daily_budget,adsets.limit(100){name,status,effective_status,daily_budget}",
             filtering=json.dumps([{"field": "name", "operator": "CONTAIN", "value": NAME_MATCH}]))
    out = []
    for c in d.get("data", []):
        if not running(c):
            continue
        if c.get("daily_budget"):
            out.append({"id": c["id"], "level": "kampanj (CBO)", "name": c["name"],
                        "budget": int(c["daily_budget"]) / 100, "match": ("campaignId", c["id"])})
        else:
            for a in c.get("adsets", {}).get("data", []):
                if running(a) and a.get("daily_budget"):
                    out.append({"id": a["id"], "level": "annonsuppsättning (ABO)", "name": a["name"],
                                "budget": int(a["daily_budget"]) / 100, "match": ("adSetId", a["id"])})
    return out


def run(juicy, juicy_token, meta_token, now, live=False):
    """juicy(token, path) -> json. Returnerar data för rapporten. Fel i enskilda skrivningar samlas i 'errors'."""
    today = now.date().isoformat()
    errors = []
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    if state.get("date") != today or state.get("v") != 2:
        state = {"v": 2, "date": today, "snap": None, "done": [], "reset": False, "ent": {}, "log": []}

    def log(t, e_id, name, act, why, **kw):
        state["log"].append({"t": t, "id": e_id, "name": name, "act": act, "why": why, **kw})

    # break-even-ROAS från Juicy, senaste 7 hela dagarna
    q = urllib.parse.urlencode({"dateFrom": (now.date() - dt.timedelta(days=7)).isoformat(),
                                "dateTo": (now.date() - dt.timedelta(days=1)).isoformat(), "groupBy": "DAILY"})
    s = juicy(juicy_token, "/api/stats/shop/profitloss?" + q)["summary"]
    costs = sum(s.get(k) or 0 for k in COST_KEYS) - (s.get("restockedCogs") or 0)
    be = s["netRevenue"] / (s["netRevenue"] - costs) if s["netRevenue"] > costs else None
    limit = round(be * MARGIN, 2) if be else None

    ents = entities(meta_token)
    hhmm = now.strftime("%H:%M")
    for e in ents:
        st = state["ent"].get(e["id"])
        if st is None:  # första gången i dag: originalbudget = nuvarande budget
            st = state["ent"][e["id"]] = {"base": e["budget"], "cur": e["budget"], "seen": e["budget"], "steps": 0,
                                          "name": e["name"], "level": e["level"]}
        elif abs(e["budget"] - st["seen"]) >= 1:  # budgeten ändrad i Meta av någon annan än skriptet
            log(hhmm, e["id"], e["name"], "manuell ändring",
                f"budgeten ändrad från {round(st['seen'])} till {round(e['budget'])} kr i Meta, fortsätter därifrån")
            st["cur"] = st["seen"] = e["budget"]
        st["meta"] = e["budget"]

    def cumulative():
        ads = juicy(juicy_token, f"/api/stats/facebook/ads?dateFrom={today}&dateTo={today}")["data"]
        res = {}
        for e in ents:
            k, v = e["match"]
            res[e["id"]] = [sum(a["adSpend"] for a in ads if a.get(k) == v),
                            sum(a["netRevenueV2"] for a in ads if a.get(k) == v)]
        return res

    # ögonblicksbild kl 07
    if now.hour >= SNAPSHOT_AT and state["snap"] is None:
        state["snap"] = {"t": hhmm, "v": cumulative()}
        skipped = [c for c in CHECKPOINTS if now.hour >= c]  # startade sent: mät från nu, inga beslut på tomt fönster
        state["done"] += skipped
        log(hhmm, "", "Alla Homelyst-kampanjer", "mätning startar",
            f"ROAS mäts från {hhmm}" + (f", kontroll {', '.join(f'{c:02d}' for c in skipped)} hoppas över" if skipped else ""))

    due = [c for c in CHECKPOINTS if now.hour >= c and c not in state["done"]]
    if due and limit and not state["reset"]:
        cp = max(due)
        state["done"] += due
        cum = cumulative()
        snap = (state["snap"] or {}).get("v", {})
        for e in ents:
            st = state["ent"][e["id"]]
            s0 = snap.get(e["id"], [0, 0])
            sp, rv = cum[e["id"]][0] - s0[0], cum[e["id"]][1] - s0[1]
            roas = rv / sp if sp > 0 else 0
            cap = st["base"] * MAX_MULT
            base = dict(spend=round(sp), roas=round(roas, 2))
            if sp < MIN_SPEND:
                log(f"{cp:02d}:00", e["id"], e["name"], "håll", f"för lite data ({round(sp)} kr spend sedan 07)", **base)
                continue
            if roas < limit:
                log(f"{cp:02d}:00", e["id"], e["name"], "håll", f"ROAS {roas:.2f} under gränsen {limit:.2f}", **base)
                continue
            if st["cur"] >= cap - 1:
                log(f"{cp:02d}:00", e["id"], e["name"], "håll", f"maxtaket {round(cap)} kr (4 × original) nått", **base)
                continue
            f = STEP_HIGH if st["cur"] >= HIGH_FROM else STEP
            new = min(round(st["cur"] * f), round(cap))
            pct = round((new / st["cur"] - 1) * 100)
            if live:
                try:
                    set_budget(meta_token, e["id"], new)
                except Exception as ex:
                    errors.append(f"{e['name']}: höjning misslyckades: {ex}")
                    log(f"{cp:02d}:00", e["id"], e["name"], "fel", f"kunde inte höja: {ex}", **base)
                    continue
            log(f"{cp:02d}:00", e["id"], e["name"], f"höj +{pct} %",
                f"ROAS {roas:.2f} ≥ {limit:.2f}, {round(st['cur'])} → {new} kr", budget=new, **base)
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
            log(hhmm, i, st["name"], "återställ", f"{round(st['cur'])} → {round(st['base'])} kr (original)",
                budget=round(st["base"]))
            st["cur"] = st["base"]
            if live:
                st["seen"] = st["base"]
        state["reset"] = ok

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    if not state["snap"]:
        nxt = f"{SNAPSHOT_AT:02d}:00 (mätning startar)"
    else:
        nxt = next((f"{c:02d}:00" for c in CHECKPOINTS if c not in state["done"]),
                   "23:30 (återställning)" if not state["reset"] else "i morgon 07:00")
    return {
        "test": not live, "highFrom": HIGH_FROM, "maxMult": MAX_MULT, "be": round(be, 2) if be else None,
        "limit": limit, "margin": MARGIN, "minSpend": MIN_SPEND, "next": nxt, "log": state["log"], "errors": errors,
        "snap": (state["snap"] or {}).get("t"),
        "ent": [{"name": st["name"], "level": st["level"], "base": round(st["base"]), "steps": st["steps"],
                 "sim": round(st["cur"]), "meta": round(st.get("meta", st["cur"]))} for st in state["ent"].values()],
    }
