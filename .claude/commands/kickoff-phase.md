---
description: Start a SPEC phase using the superpowers brainstorm -> plan -> subagent-driven-development flow
argument-hint: <phase-number>
---
We are starting Phase $ARGUMENTS (see the index in SPEC.md for its spec file under docs/spec/).

0. State the process tier (CLAUDE.md). Default to the lowest that applies.
1. Read CLAUDE.md, docs/spec/00-safety.md, the phase's spec file, and research/venue_facts.md. Nothing else.
2. A spec file exists, so skip brainstorming. Ask me only questions that SPEC.md §10 reserves for the owner or that block design; resolve the rest from the spec.
3. Use the writing-plans skill to write docs/plans/phase-$ARGUMENTS.md: <= 8 tasks, small ones batched, none under ~20 min (each <= ~1 hour), each with file scope, tests to write first, and an acceptance check.
4. Stop and show me the plan. Do not start implementation until I approve.
