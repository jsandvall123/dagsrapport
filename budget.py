"""Ändrar dagsbudgeten på EN kampanj med ±10–100 %. Körs av .github/workflows/budget.yml,
som startas av knapparna i Profit sheet (John klickar, bekräftar, sidan startar workflowen).

Spärrar:
- bara kampanjer i vårt annonskonto, och bara aktiva
- bara hela tiotal: +10 … +100 % eller −10 … −90 %
- en höjning får inte ta kampanjen över TAK kr per dag
- EXPECTED = budgeten sidan visade. Stämmer den inte längre (någon annan har ändrat) görs ingenting.
CBO: kampanjens budget ändras. ABO: varje aktiv annonsuppsättning ändras med samma procent.
DRY=true räknar bara. Lokalt: CAMPAIGN_ID=… PERCENT=20 DRY=true python3 budget.py --env ../.secrets/shopify-stores.env
"""
import os
import sys

import surf

TAK = 20000  # kr per dag, Johns övre gräns för hård skalning (26 nov 2026)
GOLV = 50


def plan(token, cid, pct, expected=None):
    if not cid.isdigit():
        raise SystemExit("Ogiltigt kampanj-id")
    if pct % 10 or not (10 <= pct <= 100 or -90 <= pct <= -10):
        raise SystemExit(f"Ogiltig procent: {pct}")
    c = surf.meta(cid, token, fields="name,account_id,effective_status,daily_budget,adsets.limit(100){name,effective_status,daily_budget}")
    if "act_" + str(c.get("account_id")) != surf.AD_ACCOUNT:
        raise SystemExit("Kampanjen ligger inte i vårt annonskonto")
    if c.get("effective_status") != "ACTIVE":
        raise SystemExit(f"Kampanjen är inte aktiv ({c.get('effective_status')})")
    if c.get("daily_budget"):
        targets = [(cid, c["name"], int(c["daily_budget"]) / 100)]
    else:
        targets = [(a["id"], a["name"], int(a["daily_budget"]) / 100) for a in c.get("adsets", {}).get("data", [])
                   if a.get("daily_budget") and a.get("effective_status") == "ACTIVE"]
    if not targets:
        raise SystemExit("Hittar ingen dagsbudget att ändra")
    old = sum(t[2] for t in targets)
    if expected is not None and abs(expected - old) > 1:
        raise SystemExit(f"Budgeten är {old:.0f} kr nu, sidan visade {expected:.0f} kr. Inget ändrat, ladda om sidan.")
    changes = [(i, n, b, max(GOLV, round(b * (1 + pct / 100) / 10) * 10)) for i, n, b in targets]
    new = sum(x[3] for x in changes)
    if pct > 0 and new > TAK:
        raise SystemExit(f"{new:.0f} kr per dag är över taket {TAK} kr. Inget ändrat.")
    return c["name"], old, new, changes


def main():
    if "--env" in sys.argv:
        for line in open(sys.argv[sys.argv.index("--env") + 1], encoding="utf-8"):
            if line.startswith("META_") and "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)
    token = os.environ["META_BUDGET_TOKEN"].strip()
    cid = os.environ["CAMPAIGN_ID"].strip()
    pct = int(os.environ["PERCENT"].strip())
    exp = os.environ.get("EXPECTED", "").strip()
    dry = os.environ.get("DRY", "").strip().lower() == "true"
    name, old, new, changes = plan(token, cid, pct, float(exp) if exp else None)
    print(f"{name}: {old:.0f} -> {new:.0f} kr per dag ({pct:+d} %)" + (" [TEST, inget ändrat]" if dry else ""))
    for i, n, b, nb in changes:
        if not dry:
            surf.set_budget(token, i, nb)
        print(f"  {n}: {b:.0f} -> {nb:.0f} kr")


if __name__ == "__main__":
    main()
