# Dagsrapport

Varje morgon kl. 07.00 (svensk tid) bygger GitHub Actions en sida med annonskostnad, omsättning och vinst för alla butiker, med siffror från Juicy.

## Vad körningen gör
1. **Kampanjkontroll.** Varje kampanj i det delade Meta-kontot ska bara räknas i rätt butik i Juicy. Regeln utgår från kampanjnamnet (`DK//ISELLA.DK//`, `HOMELYST`, `TOVALD` osv., se `owner()` i `build.py`). Fel rättas automatiskt och listas överst på sidan.
2. **Hämtar data från Juicy.** P&L per dag för de senaste 7 dagarna t.o.m. igår, plus topp 5 annonser efter spend för igår, 4 dagar och 7 dagar.
3. **Räknar om till SEK.** DKK, NOK och EUR räknas om med Riksbankens dagskurs (på helger används fredagens kurs).
4. **Publicerar sidan.** Sidan läggs på GitHub Pages bakom ett lösenord (staticrypt).

Om något går fel skickar GitHub ett mejl, och felet syns i en röd ruta överst på sidan.

## Schemat
GitHubs eget schema startade aldrig (11–12 nov 2026), så rapporten väcks av **cron-job.org** (Johns konto, jobb "Dagsrapport") varje dag kl. 07:00 Europe/Stockholm. Jobbet gör POST till
`https://api.github.com/repos/jsandvall123/dagsrapport/actions/workflows/daily.yml/dispatches` med body `{"ref":"main"}` och en fine-grained GitHub-nyckel (`dagsrapport-trigger`, bara Actions read/write på detta repo). **Nyckeln går ut 12 nov 2027** och måste då förnyas och bytas i cron-job.org. GitHubs cron i workflowen ligger kvar som reserv.

## Nycklar
Alla nycklar ligger som GitHub-secrets i repot, aldrig i koden:
`JUICY_<BUTIK>_TOKEN` (SE, DK, NO, FI, DE, HOMELYST, HUSTOVALD), `DAGSRAPPORT_PASSWORD`, `DAGSRAPPORT_SALT`.
Lokalt finns samma värden i `../.secrets/shopify-stores.env`.

## Ny butik
1. Skapa en Juicy-token i butiken (Juicy → Settings → Connections → AI & API access → Create token).
2. Lägg till butiken i `STORES` (och vid behov i `owner()`) i `build.py`.
3. Lägg in nyckeln som secret: `gh secret set JUICY_<KOD>_TOKEN`.

## Köra för hand
- På GitHub: Actions → Dagsrapport → Run workflow.
- Lokalt utan att ändra något i Juicy: `python3 build.py --env ../.secrets/shopify-stores.env --dry`, öppna sedan `site/index.html`.
