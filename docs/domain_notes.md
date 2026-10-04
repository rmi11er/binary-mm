# Domain notes (pricing and quoting)

Moved from CLAUDE.md. Methodology detail: [docs/spec/20-pricing.md](spec/20-pricing.md).

- Digital = −dC/dK. Prefer model-free call-spread anchors; Black-76 delta/gamma only for between-anchor updates.
- Negative equity skew makes upside digitals worth more than flat-vol N(d2), by about 1–2¢ near ATM on 1DTE. Do not "simplify" skew away.
- Gamma explodes near the strike as T→0: quoting must stop before resolution (default T−15min).
- Incentive pools are pro rata and finite. Report incentive P&L separately and never let it mask negative markouts.
