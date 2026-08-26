# Working session protocol

## At the beginning of a work session

1. Read `PROJECT.md`.
2. Read `STATE.md`.
3. Read relevant parts of `ARCHITECTURE.md`.
4. Check `DECISIONS.md` for decisions related to the task.
5. Check relevant TODOs.
6. **Inspect the actual repository before assuming the documentation is correct.**

## During work

- Use the repository as the source of truth.
- Record significant new architectural/design decisions.
- Do not continuously write progress commentary into MD files.
- Add TODOs only for concrete unfinished work.

## Before finishing a substantial work session

- Run appropriate tests (see `TESTING.md`).
- Update `STATE.md`.
- Update `TODO.md` if priorities changed.
- Add a `DECISIONS.md` entry if a significant decision was made.
- Update `ARCHITECTURE.md` only if architecture actually changed.
- Update `TESTING.md` only if verification knowledge changed.
- Update `PROJECT.md` only if requirements/scope changed.
- Remove obsolete state information.
- Check that documentation does not contradict the repository.

## Project-specific cautions

These have cost real time before. Read them once per session.

**This system trades real funded accounts.** `nt8/` is a live-trading path. Any
change to entry, exit, order state or routing needs the checks in `TESTING.md`
before it is enabled on the `LIVE` book.

**C# in `nt8/` cannot be compiled by NinjaTrader from here.** Use the Roslyn
syntax check in `TESTING.md`. The user must recompile in the NinjaScript Editor
before any fix takes effect — say so explicitly when handing over a change.

**Do not "fix" a divergence from the original strategy without checking
`DECISIONS.md` first.** Several apparent defects are deliberate fidelity choices,
and at least one apparent improvement was reverted for that reason.

**Heredoc escaping bites repeatedly.** Writing Python/C#/Markdown through a bash
heredoc collapses one level of backslashes; `\n` inside a replacement string and
Windows paths such as `\NinjaTrader` become syntax errors. Use raw strings, build
newlines with `chr(10)`, or use the file-write tool for anything non-trivial.

**Verify claims against the logs before proposing a fix.** Two separate
"discrepancies" in this project turned out to be my own reconstruction errors
(see `DECISIONS.md` 2026-08-26 entries). The routing CSV and the NinjaTrader
Control Center log are authoritative; the Output window is not.
