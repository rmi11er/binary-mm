---
description: Run risk-auditor and quant-reviewer on the current diff and summarise blockers
---
Run `git diff main...HEAD --stat` and `git diff main...HEAD`.
Dispatch the risk-auditor subagent on any changed files under src/bmm/{risk,gateway,quoter,hedger} or cli/config,
and the quant-reviewer subagent on changed files under src/bmm/{pricing,surface,quoter,analytics}, in parallel.
Summarise: APPROVE/BLOCK and PASS/FAIL, then a merged, deduplicated list of required fixes.
