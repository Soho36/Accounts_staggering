# Project memory

Long-term context for AI-assisted work on this repository. Read
`BEGINNING_OF_A_WORK_SESSION.md` first; it says what to read and in what order.

| File | Answers | Update frequency |
|---|---|---|
| `GENERAL_RULES_FOR_ALL_MD_FILES.md` | how to maintain these files | rarely |
| `BEGINNING_OF_A_WORK_SESSION.md` | session protocol, project-specific cautions | rarely |
| `PROJECT.md` | what this project is and its invariants | rarely |
| `ARCHITECTURE.md` | how the system fits together | on architectural change |
| `DECISIONS.md` | why the system is like this | on significant decisions |
| `STATE.md` | where we are right now | frequently; rewrite, don't append |
| `TODO.md` | what remains to be done | continuously, selectively |
| `TESTING.md` | how to verify a change works | when verification knowledge changes |

## Operator manuals live elsewhere and are not duplicated here

| File | Role |
|---|---|
| `nt8/README.md` | deployment, per-chart settings, release-gate checklist |
| `nt8/FIRST_START.md` | first-start and peak-seeding procedure |
| `nt8/ROUTER_LOGIC.md` | how allocation works; how to read a log |
| `README.md` (root) | the Python simulator |
| `results/*.md` | generated study output — regenerate, never hand-edit |
