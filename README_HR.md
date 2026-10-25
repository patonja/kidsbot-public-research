# KIDSBOT V5 — neovisni javni povijesni Freqtrade backtest

V4 je pravilno stao: LIVE VPS ima Binance botove i ključeve. **Nemoj** uklanjati zaštitu niti izvoditi vanjske strategije na LIVE VPS-u.

V5 je za **novi javni GitHub repozitorij bez ikakvih tajni, API ključeva, korisničkih podataka ili Binance autorizacije**. GitHub Actions pokreće zaseban privremeni poslužitelj. Standardni GitHub-hosted runneri trenutačno su besplatni za javne repozitorije (uvjeti se mogu promijeniti).

## Pokretanje

1. Kreiraj prazan **public** GitHub repozitorij npr. `kidsbot-public-research`; **nikad** ne kopiraj `/root` ili svoje Binance datoteke u taj repo.
2. Dodaj `.github/workflows/kidsbot_research.yml` i `research/run_batch.py` iz ovog paketa, zadržavajući putanje. `README_HR.md` je neobavezan.
3. Otvori repozitorij → **Actions** → **KIDSBOT V5 Public Research** → **Run workflow**.
4. Na završetku, u runu preuzmi artifact **kidsbot-v5-screening** i pregledaj `summary.json`; možeš zalijepiti sekciju `V5 FINAL` u chat.

Nema potrebe SSH-om raditi bilo što na LIVE VPS-u.

## Izvor i metoda

- GitHub public repository `freqtrade/freqtrade-strategies` na fiksnom commit-u `f3340ce11f5bdf62f598522e64d1f5638eaa13f5`.
- Izvorne klase: ADXMomentum, AdxSmas, AwesomeMacd, BbandRsi, Freqtrade_backtest_validation_freqtrade1, HourBasedStrategy, Supertrend. **Zadnje dvije mogu biti pokazne/testne, ne ekonomske strategije**.
- Binance javne mjesečne arhive: pet USDC spot parova, 1h svijeće, siječanj–rujan 2025. UTC.
- **Bitno:** Spot ZIP podaci iz 2025. imaju mikrosekundne timestampove; V5 ih normalizira u milisekunde. Provjerava Binance SHA-256 CHECKSUM i broj svijeća.
- Freqtrade originalne Python klase izvršavaju se **samo unutar ograničenog Docker kontejnera na privremenom research runneru**, ne na LIVE VPS-u. Kontejner ima mrežni pristup samo zbog javnog API/market metapodataka; svejedno ne daje nikakva jamstva protiv svih rizika untrusted koda.
- Freqtrade naknada 0,1% **po strani**, pa dodatno konzervativno modelira 0,1% po strani za spread i slippage; jedan ulazni kapital 16,82 USDC, ulog 10 USDC, maksimalno jedna aktivna pozicija.
- Povijesni screening **NIJE** dokaz LIVE isplativosti. Puni 2025 Oct-Dec holdout namjerno **nije ni preuzet ni otvoren**.
- Nedostajući paket, netočan podatak, zaštićeni API poziv, greška izvornog koda ili nedostupno tržište → **FAIL CLOSED / ENGINE_ERROR**, ne uljepšani rezultat. Ponekad službene klase ne rade na modernom Freqtradeu zbog promjena API-ja; to nije negativan rezultat strategije.
- Minimalna veličina naloga, likvidnost, spread po trenutku, OCO izlaz i neovisni forward podaci nisu dokazani. **Nema dozvole za LIVE trgovanje.**

## Napomene o kompatibilnosti

Ako GitHub Actions runner dobije geografsko odbijanje Binance javnih API-jeva prilikom učitavanja market metadata, Freqtrade može završiti u `ENGINE_ERROR`; ZIP povijest je već dostupna preko data.binance.vision, ali metapodaci burze zaseban su API zahtjev. Ne mijenjati burzu ni parove samo da se dobije pozitivan rezultat. `summary.json` zadržava stvarni status.
