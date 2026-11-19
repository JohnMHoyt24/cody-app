# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Python CLI code-review agent powered by Google Gemini. Given a single source file, it produces a markdown report with:

1. What was done well.
2. What could be improved.
3. Bugs or syntax errors.
4. *(Optional, `--rewrite`)* A rewritten version of the file plus a unified diff against the original.

If the input isn't source code (plain English, an invented language), the agent says so and skips the review sections.

## Layout

- [my-agent/review.py](my-agent/review.py) — CLI entry point. Parses args, reads the file, runs the review (and optional rewrite) behind a spinner with an elapsed-time counter, then renders the report in the terminal and offers to export it as `.md`, or writes it directly with `-o`.
- [my-agent/gemini_client.py](my-agent/gemini_client.py) — the only module that talks to Gemini. Loads config from `.env`, builds the client (timeouts, retries on 429/500/503), and maps every SDK/network exception to `AgentError`. Defines the `Review` pydantic model, which also serves as Gemini's `response_schema`.
- [my-agent/prompts.py](my-agent/prompts.py) — the `REVIEW_SYSTEM` and `REWRITE_SYSTEM` system prompts.
- [my-agent/report.py](my-agent/report.py) — pure formatting. Turns a `Review` plus an optional rewrite into markdown and builds the diff with `difflib`. No API calls.
- [my-agent/README.md](my-agent/README.md) — user-facing setup and usage docs, including the options table.
- [my-agent/requirements.txt](my-agent/requirements.txt) — `google-genai`, `httpx2`, `pydantic`, `python-dotenv`, `rich`. List every package the code imports directly, even when another dependency already pulls it in.
- [samples/](samples/) — five test inputs covering the main cases: broken syntax (Python), inefficient but working (shell), mostly good (JS), a nonsensical language (`.borf`), and an English paragraph.

## Running

```powershell
.venv\Scripts\Activate.ps1
pip install -r my-agent/requirements.txt
python my-agent/review.py samples/01_broken_syntax.py
python my-agent/review.py samples/03_pretty_good.js --rewrite -o report.md
```

Run from the repo root. `.env` is found by searching upward from the current working directory, and it must define `GEMINI_API_KEY` (optionally `GEMINI_MODEL`; the default is `gemini-3.5-flash`). `.env` is gitignored, so never commit it.

There is no test suite or build step. To check a change, run the CLI against the files in [samples/](samples/) and make sure each case behaves as expected (non-code inputs should get the "not source code" notice).

## Design notes and conventions

- **Two API calls at most.** Sections 1–3 come from one structured-JSON call constrained by the `Review` schema. Section 4 is a second, optional call that returns a fenced code block, extracted with the `_FENCE` regex.
- Source is sent with line-number prefixes for the review call (so the model can cite `L12`), but **without** them for the rewrite call, so the numbers don't leak into the rewritten code.
- Keep SDK exceptions inside `gemini_client.py`. Everything else catches only `AgentError`, which `main()` prints as a single line and turns into exit code 1 (130 on Ctrl+C).
- Status/spinner output goes to stderr and the report goes to stdout, so `review.py file > out.md` stays clean. The export prompt is skipped when stdin or stdout isn't a TTY.
- `--thinking` defaults to `low`. Higher Gemini 3.x thinking levels can take minutes per call.
- New language-to-fence-tag mappings go in `_FENCE_TAGS` in [report.py](my-agent/report.py). Unknown languages fall back to `text`.
- Code is documented with docstrings and explanatory comments throughout. Keep that style when adding to it.
