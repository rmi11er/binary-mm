# Spec 00: Safety and testing standards

> Part of the binary-mm spec. Index: [`SPEC.md`](../../SPEC.md). Section numbers are the original SPEC numbering.

## 3. Hard safety rules (non-negotiable — enforced in code and in review)

1. **Live trading is opt-in at three layers:** `BMM_MODE=live` env var, `--live` CLI flag, and `config/live.toml` containing `i_understand_this_trades_real_money = true`. Missing any of them → paper/sim only. Default is `record`.
2. IB Gateway ports: paper 4002, live 4001. Code must refuse to connect to 4001 unless rule 1 holds.
3. **Every order passes `risk.check(order, state)`.** There is no code path to the broker that bypasses it. A test asserts this (grep plus unit test on the gateway wrapper).
4. **Limits in v1 config (set by owner, not by agents):**
   - max contracts per order: 10 (forward-test lots)
   - max net YES exposure per strike: 100 contracts
   - max gross collateral at risk: $2,000
   - max |net digital gamma| inside the final 60 min before resolution: config value; quoting **stops** at T−15 min by default
   - daily loss stop: $150 → cancel all, flatten delta, halt until manual restart
5. **Stale-data kill:** if the reference futures feed has no update for > 3s during RTH, or options anchors are older than the configured max age → cancel all quotes.
6. **Event blackout:** no resting quotes from T−60s to T+120s around scheduled releases (CPI, NFP, FOMC, etc., from a calendar file), or around the cash open and close if configured.
7. Global kill switch: `bmm kill` CLI command, plus a file-based flag (`/var/run/bmm.kill`) checked every loop.
8. Secrets only from env or `.env` (gitignored). Never log credentials. Never commit `.env`.
9. Agents **never** change values in `config/limits.toml` or `config/live.toml`, and never run anything with `--live`. Only the owner does.

## 8. Testing standards
- TDD for pricing, risk and quoter (superpowers `test-driven-development`).
- Property tests (hypothesis): quotes always inside [0.01, 0.99], bid < ask, ForecastEx translation consistent (YES ask a ⇔ NO bid 1−a), risk.check never approves beyond limits.
- Golden-file tests for recorder schemas.
- Any change touching `risk/`, `gateway/` order methods, or `quoter/` requires **risk-auditor** subagent sign-off recorded in the PR description.
