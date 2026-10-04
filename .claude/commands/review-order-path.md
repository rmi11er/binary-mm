---
description: Run risk-auditor and quant-reviewer on the current diff and summarise blockers
---
Run `git diff main...HEAD --stat`, then write `git diff main...HEAD` to a file under the scratchpad (do not print it).
Tier A (src/bmm/{risk,quoter,hedger}, gateway order methods): dispatch risk-auditor AND quant-reviewer in parallel.
Tier B (read-only tools): quant-reviewer only, and only if the diff touches math.
Each reviewer gets the diff (save it to a file) plus the single spec file the change implements, never the whole repo.
Summarise: APPROVE/BLOCK and PASS/FAIL, then a merged, deduplicated list of required fixes.
