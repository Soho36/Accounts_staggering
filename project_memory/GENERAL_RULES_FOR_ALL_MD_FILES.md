# General rules for all MD files

Treat code, tests, configuration, and actual repository state as the ultimate
source of truth.

Keep documentation concise enough that an agent can read the important files at
the beginning of a work session.

## Size rule

There is no strict line limit, but optimize these files for information density
rather than completeness.

A useful principle:

> If an AI can cheaply rediscover something by inspecting the code, it probably
> does not belong in long-term project memory.

Conversely:

> If rediscovering something would require hours of investigation,
> experimentation, or understanding why an earlier approach failed, it is an
> excellent candidate for documentation.

The objective is not to make the AI read more context. It is to give it the
smallest amount of context that prevents expensive rediscovery and repeated
mistakes.

## Content rules

- Record **why** something was done when the reason is not obvious from the code.
- Prefer facts and decisions over long narratives.
- Do not record every command, experiment, or minor code change.
- Do not duplicate information across several MD files unless necessary.
- Link to relevant files/classes/modules instead of copying large pieces of code.
- Use dates for decisions and significant state changes.
- Clearly distinguish between:
  - confirmed facts
  - current assumptions
  - unresolved questions
  - planned work
- Remove or archive information that is no longer useful.
- If documentation conflicts with the repository, investigate the discrepancy
  rather than blindly following the MD file.

## Keep STATE.md disposable

STATE.md should be aggressively rewritten.

Bad:

> On August 3 we implemented X. Then on August 5 we discovered Y. On August 6 we
> tried Z...

Better:

> Current implementation uses Z. X was abandoned because of Y.

If the history contains an important lesson, move that lesson into DECISIONS.md.

## Preserve reasons, discard chronology

Long-term memory should answer *why is the system like this?*

It usually does not need to answer *what exactly did we do at 14:37 three months
ago?* Keep the former. Delete the latter.

## Avoid automatic documentation spam

Do **not** update all MD files after every task. Instead, after completing a
substantial task, determine which project-memory files have materially changed
and update only those. Often the correct number of updated files is zero or one.

## Clean documentation, not merely append to it

For STATE.md and TODO.md: remove obsolete information, replace old state
descriptions, consolidate duplicate entries, shorten verbose notes.

DECISIONS.md is different: previous important decisions normally remain, because
they contain historical reasoning.

## Periodically perform a documentation audit

Every major milestone — or roughly every few weeks in an active project — check:

- Does PROJECT.md still describe the actual project?
- Does ARCHITECTURE.md match the current architecture?
- Are any decisions obsolete or superseded?
- Does STATE.md describe the present rather than the past?
- Does TODO.md contain dead tasks?
- Does TESTING.md still describe the real verification process?
- Is information duplicated unnecessarily?
- Could any section be shorter without losing important knowledge?

Mark superseded decisions rather than silently deleting them when their history
remains useful:

```
SUPERSEDED 2026-11-14 by Decision #27
```

This prevents an agent from encountering two contradictory decisions without
knowing which one is current.

## Relationship to the operational manuals

These project-memory files are **not** the operator manuals. The following stay
as they are and are linked, never duplicated:

| File | Role |
|---|---|
| `nt8/README.md` | deployment, settings, release-gate checklist |
| `nt8/FIRST_START.md` | step-by-step first-start / peak-seeding procedure |
| `nt8/ROUTER_LOGIC.md` | how allocation decisions work; how to read a log |
| `results/*.md` | generated study output; regenerate, do not hand-edit |
| `README.md` (root) | the Python simulator's own documentation |
