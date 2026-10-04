# Product shortlist (Phase 0, 2026-10-03)

Input to Phase 3, not a final list; decisions do not pre-empt SPEC section 10.1. Every claim traces to a row of `research/venue_facts.md`; open items are numbered in `research/owner_checklist.md` ("owner item #N"). Incentive standing for every ForecastEx candidate: the LIP Program Period and the Financials retainer both ended 2026-09-30 and renewal is unconfirmed (owner item #1), so no GO below relies on incentives; each GO rests on the pricing thesis (negative equity skew and slow repricing versus an options surface, per CLAUDE.md domain notes, not a Phase 0 finding) plus a reference whose instrument, roll and settlement value are specified from primary text (the observation clock is owner item #7). ForecastEx fee: $0.01 per contract per executed side (venue_facts "Commission"); against a $0.01 tick every execution costs about one tick and an open-and-flatten round trip is two executions. Whether IBKR adds anything on top is owner item #3.

### ForecastEx — S&P 500 daily close
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX); product code FES
- Reference price: CME E-mini S&P 500 futures daily settlement price (first number under SETT. PRICE in table EMINI S&P FUT of the CME daily bulletin) for the contract month named in the question, as determined by CME (method pending owner item #7); not cash SPX. Roll: lead month, or second month on days the lead month goes through final settlement at CME.
- Reference time: Outcome fixed (T anchor): the CME settlement on the question date, last trade 3:00 PM CT (4:00 PM ET) on that date; exact settlement clock time pending owner item #7; T and the SPEC section 3 windows must run to this, not to resolution (owner item #20). Resolution/payout (separate): resolution 10:00 AM CT (11:00 AM ET) on the business day after the settlement date when CME publishes the final bulletin; payout 1:00 PM CT same day; no early resolution. Terms silent on DST (owner item #6).
- Strike grid: not in the terms, set per listing at ForecastEx discretion; needs owner (owner item #10). Tick $0.01, payout $1.00.
- Incentive eligibility: FES is in the LIP Appendix A (Target Size 5000) and the Financials retainer, but the LIP Program Period 2026-09-01 to 2026-09-30 and the retainer term have ended and renewal is unconfirmed (owner item #1); retainer amounts unpublished (owner item #15); underwrite at about half per CFTC advisory 26-23, and the decision assumes zero. Fee $0.01 per side per contract applies to every fill.
- Hedge: MES. Futures-referenced, so F = the referenced E-mini S&P lead-month futures (second month on the final-settlement-day roll) with no carry (SPEC §6.4). Whether MES settlement tracks the E-mini settlement for the hedge is owner item #7.
- Market data: needs CME futures plus options-surface data for the E-mini S&P and MES, and FORECASTX quotes; prices account-dependent, owner to confirm (items #4, #5).
- Decision: GO — instrument, roll and settlement value specified from primary text and the pricing thesis applies; does not depend on incentives; the observation clock (owner item #7) and strike grid (owner item #10) remain open.
- Sources: https://www.cftc.gov/sites/default/files/filings/orgrules/25/05/rules05262522278.pdf ; https://www.cftc.gov/sites/default/files/filings/orgrules/25/05/rules05262522277.pdf ; https://www.cftc.gov/filings/orgrules/rules03242641956.pdf ; https://www.cftc.gov/filings/orgrules/rules08182618342.pdf

### ForecastEx — Nasdaq-100 daily close
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX); product code FNQ
- Reference price: CME E-mini Nasdaq-100 futures official daily settlement price (first number under SETT. PRICE in table EMINI NASD FUT of the CME daily bulletin) for the contract month named in the question, as determined by CME (method pending owner item #7); not cash NDX. Roll: lead month, or second month on days the lead month goes through final settlement at CME.
- Reference time: Outcome fixed (T anchor): the CME settlement on the question date, last trade 3:00 PM CT (4:00 PM ET) on that date; exact settlement clock time pending owner item #7; T and the SPEC section 3 windows must run to this, not to resolution (owner item #20). Resolution/payout (separate): resolution 10:00 AM CT (11:00 AM ET) on the business day after the settlement date (Monday for a Friday date) when CME publishes the final bulletin; payout 1:00 PM CT same day; no early resolution. Terms silent on DST (owner item #6).
- Strike grid: not in the terms, set per listing at ForecastEx discretion; needs owner (owner item #10). Tick $0.01, payout $1.00.
- Incentive eligibility: FNQ is in the LIP Appendix A (Target Size 5000) and the Financials retainer, but the LIP Program Period and the retainer term ended 2026-09-30 and renewal is unconfirmed (owner item #1); underwrite at about half per CFTC advisory 26-23, and the decision assumes zero. Fee $0.01 per side per contract applies to every fill.
- Hedge: MNQ. Futures-referenced, so F = the referenced E-mini Nasdaq-100 lead-month futures (second month on the final-settlement-day roll) with no carry (SPEC §6.4). Whether MNQ settlement tracks the E-mini settlement for the hedge is owner item #7.
- Market data: needs CME futures plus options-surface data for the E-mini Nasdaq-100 and MNQ, and FORECASTX quotes; prices account-dependent, owner to confirm (items #4, #5).
- Decision: GO — instrument, roll and settlement value specified from primary text and the pricing thesis applies (skew and slow repricing versus the NQ options surface); does not depend on incentives; the observation clock (owner item #7) and strike grid (owner item #10) remain open.
- Sources: https://www.cftc.gov/sites/default/files/filings/orgrules/25/05/rules05262522278.pdf ; https://www.cftc.gov/sites/default/files/filings/orgrules/25/05/rules05262522277.pdf ; https://www.cftc.gov/filings/orgrules/rules03242641956.pdf ; https://www.cftc.gov/filings/orgrules/rules08182618342.pdf

### ForecastEx — Gold price
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX); product code METLG
- Reference price: official CME settlement price of the Gold Futures contract named in the question (Settlement column, Resolution Date only); not spot or LBMA fix. Roll: each contract is designated front-month (nearest listed, through its final settlement date) or active-month (CME-designated nearest active month that is not the front month); designation read per contract.
- Reference time: Outcome fixed (T anchor): the CME settlement on the Resolution Date, last trade 12:30 PM CT (1:30 PM ET) on that date; exact settlement clock time pending owner item #7; T and the SPEC section 3 windows must run to this, not to resolution (owner item #20). Resolution/payout (separate): resolution 11:55 PM CT (12:55 AM ET next day) on the listed date; payout 1:00 PM CT the day after resolution; no early resolution. DST not stated (owner item #6).
- Strike grid: not in the terms, set per listing at ForecastEx discretion; needs owner (owner item #10). Tick $0.01, payout $1.00.
- Incentive eligibility: METLG is in the LIP Appendix A (Target Size 200) and the Financials retainer, but both ended 2026-09-30 and renewal is unconfirmed (owner item #1); underwrite at about half per CFTC advisory 26-23, and the decision assumes zero. Fee $0.01 per side per contract applies to every fill.
- Hedge: MGC. Futures-referenced, so F = the named Gold Futures contract month with no carry (SPEC §6.4); each question names the month, so the hedge month follows the question and the front-month versus active-month designation. Whether MGC lists and tracks the named month is owner item #7.
- Market data: needs COMEX futures plus gold options-surface data and FORECASTX quotes; prices account-dependent, owner to confirm (items #4, #5).
- Decision: GO — provisional: named futures contract, designation rule and settlement value are specified from primary text and the decision does not depend on incentives; whether a skew edge exists on gold is untested (the equity-skew thesis does not transfer by analogy) and is an open Phase 3 question; observation clock is owner item #7.
- Sources: https://www.cftc.gov/filings/orgrules/rules07282612138.pdf ; https://www.cftc.gov/filings/orgrules/rules03242641956.pdf ; https://www.cftc.gov/filings/orgrules/rules08182618342.pdf

### CME Event Contracts — Gold (ECGC)
- Venue: CME Globex via IBKR (secType FOP, trading class ECGC); COMEX Chapter 23 on COMEX Gold Futures (GC)
- Reference price: daily settlement price of the Principal Contract Month of COMEX Gold Futures at Expiration (Rule 2302.G); Call pays if strictly above the strike, Put if at or below. Roll rule for the Principal Contract Month not found (Strike Price Listing and Exercise Procedures Table not read).
- Reference time: settlement price calculated by CME at a time determined by the Exchange, times default to Central; gold settlement window 12:29 to 12:30 PM CT is only a search excerpt; DST not stated (owner items #7, #17).
- Strike grid: gold strike schedule not found (owner item #17). Payout $1.00, tick $0.01 for contracts listed from trade date 2025-12-08.
- Incentive eligibility: CME Event Contracts Market Maker Program exists (up to 7 staff-selected proprietary firms) but current terms are unknown (owner item #18); not a ForecastEx program, and the ForecastEx LIP ended 2026-09-30 (owner item #1); no incentive income underwritten. CME Globex fee $0.01 per contract plus IBKR lines unverified (owner item #16).
- Hedge: MGC. Futures-referenced, so F = the Principal Contract Month Gold Futures with no carry (SPEC §6.4), but the month cannot be identified until the roll rule is read.
- Market data: needs COMEX data (futures plus options surface); price owner to confirm (item #5).
- Decision: NO-GO — pending owner item #17 (roll rule and settlement window open; also item #7)
- Sources: https://www.cftc.gov/sites/default/files/filings/orgrules/24/01/rules01092410601.pdf ; https://www.cftc.gov/sites/default/files/filings/orgrules/25/12/rules12022533903.pdf ; https://www.cftc.gov/sites/default/files/filings/orgrules/25/01/rules01102513644.pdf

### CME Event Contracts — Nasdaq-100 (ECNQ)
- Venue: CME Globex via IBKR (secType FOP, trading class ECNQ); CME Chapter 23 on E-mini Nasdaq-100 Futures (NQ)
- Reference price: daily settlement price of the Principal Contract Month of E-mini Nasdaq-100 Futures at Expiration (Rule 2302.G); Call pays if strictly above the strike, Put if at or below. Roll rule for the principal month not read; extra contracts expire on the last US business day of the nearest quarter and of the calendar year (Jan 2024 filing).
- Reference time: calculated by CME at a time determined by the Exchange, Central by default; the NQ settlement window is unestablished (two excerpts disagree) and DST not stated (owner items #7, #17).
- Strike grid: Jan 2024 schedule (500-point increments widening to 10-point near expiry; may be superseded); current grid to confirm (owner item #17). Payout $1.00, tick $0.01 from trade date 2025-12-08.
- Incentive eligibility: CME Event Contracts Market Maker Program current terms unknown (owner item #18); ForecastEx LIP ended 2026-09-30 and is not applicable (owner item #1); no incentive income underwritten. CME Globex fee $0.01 per contract plus IBKR lines unverified (owner item #16).
- Hedge: MNQ. Futures-referenced, so F = the Principal Contract Month NQ futures with no carry (SPEC §6.4); month unidentified until the roll rule is read.
- Market data: needs CME data (futures plus options surface); price owner to confirm (item #5).
- Decision: NO-GO — pending owner item #17 (roll rule and settlement window open; also item #7)
- Sources: https://www.cftc.gov/sites/default/files/filings/orgrules/24/01/rules01092410601.pdf ; https://www.cftc.gov/sites/default/files/filings/orgrules/25/12/rules12022533903.pdf ; https://www.cftc.gov/sites/default/files/filings/orgrules/25/01/rules01102513644.pdf

### ForecastEx — Crypto (BTC, ETH, XRP; CF Benchmarks period high/low)
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX); codes YXH or YXL plus a 2-letter crypto code
- Reference price: highest or lowest per-minute trimmed mean of the CF Benchmarks Real-Time Index over the period (extract only, [S]).
- Reference time: resolution 11:59 PM CT on the last day of the period (extract only, [S]).
- Strike grid: not found.
- Incentive eligibility: crypto codes appear in LIP Appendix A (Target 2000), but the Program Period ended 2026-09-30 and renewal is unconfirmed (owner item #1); fee $0.01 per side per contract.
- Hedge: not applicable in v1 (crypto hedger and feeds out of scope).
- Market data: not scoped; no subscription proposed.
- Decision: NO-GO (v1) — hedger/feeds out of scope (SPEC §4)
- Sources: https://data.forecastex.com/regulatory/YXTermsandConditions.pdf ; https://www.forecastex.com/contracts ; https://www.cftc.gov/filings/orgrules/rules08182618342.pdf

### ForecastEx — Economic indicators and temperature (group)
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX)
- Reference price: econ-data and daily/hourly temperature products; no contract text was collected for them in Phase 0 beyond retainer programs naming them (Economic Indicators retainer, tiered since 2026-09-21).
- Reference time: not collected.
- Strike grid: not collected.
- Incentive eligibility: Economic Indicators retainer amended effective 2026-09-21 (amounts unpublished, owner item #15); LIP and Financials retainer ended 2026-09-30 with renewal unconfirmed (owner item #1); not relied upon.
- Hedge: none; no futures or options instrument to hedge or derive F from.
- Market data: none proposed.
- Decision: NO-GO — no options surface to price from
- Sources: https://www.cftc.gov/filings/orgrules/rules09042624145.pdf ; https://www.cftc.gov/filings/orgrules/rules08282622743.pdf

### ForecastEx — US Industrial 30, US Small Cap 2000, silver, platinum, palladium (group)
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX); codes FYM, FRTY, METLS, METLP, METLD
- Reference price: FYM CME E-mini Dow futures daily settlement (lead month; primary T&C); FRTY CME E-mini Russell 2000 futures daily settlement (lead month; [S], proxy summary); METLS, METLP, METLD official CME settlement of the named futures contract ([S], proxy summary).
- Reference time: FYM same as FES (last trade 3:00 PM CT, resolution 10:00 AM CT); FRTY and the other metals not examined.
- Strike grid: not found; discretionary.
- Incentive eligibility: METL products (Target 200) are in LIP Appendix A, but the Program Period ended 2026-09-30 and renewal is unconfirmed (owner item #1).
- Hedge: no MES/MNQ/MGC instrument covers these underlyings; v1 hedge set not extended.
- Market data: none proposed.
- Decision: NO-GO (v1) — no hedge instrument in the MES/MNQ/MGC set and FRTY and other-metals terms are [S]; revisit after the GO set is running
- Sources: https://www.cftc.gov/sites/default/files/filings/orgrules/25/05/rules05262522278.pdf ; https://www.cftc.gov/sites/default/files/filings/ptc/25/07/ptc07212526910.pdf ; https://www.cftc.gov/filings/orgrules/rules07282612138.pdf

### ForecastEx — EURO STOXX 50, DAX 40, copper, WTI crude, natural gas, Gold Comparison (group)
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX)
- Reference price: listed on the contracts page but no contract terms located (needs owner).
- Reference time: not found.
- Strike grid: not found.
- Incentive eligibility: copper, WTI and Henry Hub rows (and Brent) appear in LIP Appendix A, but the Program Period ended 2026-09-30 and renewal is unconfirmed (owner item #1).
- Hedge: not specified; no instrument named.
- Market data: none proposed.
- Decision: NO-GO — pending owner item #19
- Sources: https://www.forecastex.com/contracts ; https://www.cftc.gov/sites/default/files/filings/ptc/26/01/ptc01292638497.pdf ; https://www.cftc.gov/filings/orgrules/rules08182618342.pdf

### ForecastEx — 10-Year Treasury yield and ECB FX rate contracts (group)
- Venue: ForecastEx (via IBKR, secType OPT, exchange FORECASTX); 10YRY and UST yield contracts, ECB FX contracts
- Reference price: Federal Reserve H.15 daily yield (UST); ECB reference rate (FX); both from proxy extracts of the T&C ([S]).
- Reference time: UST H.15 published 3:15 PM CT, last trade 2:14:59 PM CT; ECB FX last trade 14:10 CET (CET versus CEST handling not stated).
- Strike grid: not found; discretionary for FX, 9 expirations to 24 months for UST (extract).
- Incentive eligibility: LIP Appendix A lists 5YRY, 20YRY, 30YRY and USEUR (Target 200 each) but not 10YRY; LIP ended 2026-09-30 and renewal is unconfirmed (owner item #1).
- Hedge: no MES/MNQ/MGC instrument; rate and FX hedgers are not in the v1 set.
- Market data: none proposed.
- Decision: NO-GO (v1) — reference is a published rate, not a futures settle; no hedge instrument in the v1 set and terms are [S]
- Sources: https://data.forecastex.com/regulatory/USTTermsandConditions.pdf ; https://data.forecastex.com/regulatory/ECBFXRateTermsandConditions.pdf

## Budget

Cap: $150 per month for IBKR data subscriptions (SPEC §1). GO set: ForecastEx S&P 500 (FES), Nasdaq-100 (FNQ), Gold (METLG).

| Subscription needed | Why | Monthly cost |
|---|---|---|
| CME E-mini S&P 500 and MES futures data, plus S&P options surface | F, hedge, skew anchors for FES | owner to confirm (item #5) |
| CME E-mini Nasdaq-100 and MNQ futures data, plus NQ options surface | F, hedge, skew anchors for FNQ | owner to confirm (item #5) |
| COMEX gold futures (GC, MGC) and gold options surface | F, hedge, skew anchors for METLG | owner to confirm (item #5) |
| FORECASTX quotes (top of book; depth not documented) | quoting and recorder | owner to confirm (item #4); no ForecastEx data fee found in the rulebook, IBKR fee not found |

Sum versus the $150 cap: cannot be computed from Phase 0 facts because every price is account-dependent; owner to confirm (items #4, #5). Non-professional status (item #4) changes the prices. No CME Event Contracts entitlement is needed for the GO set because both CME EC candidates are NO-GO pending owner items #7 and #17. ForecastEx data may not be redistributed or resold (Rules 411(a), 901(b)); recorder output stays internal.
