# my-agent: Gemini code-review CLI

Reviews one source file and reports:

1. What was done well
2. What could be improved
3. Bugs or syntax errors
4. *(with `--rewrite`)* A rewritten version, plus a diff against the original

If the input isn't source code (for example plain English or a made-up language), the agent says so and skips the rest.

## Setup (Windows PowerShell)

```powershell
.venv\Scripts\Activate.ps1
pip install -r my-agent/requirements.txt
```

Credentials are read from the `.env` file at the repo root:

```
GEMINI_API_KEY=your-key-from-aistudio.google.com
GEMINI_MODEL=gemini-3.5-flash   # optional
```

## Usage

```powershell
python my-agent/review.py samples/01_broken_syntax.py
python my-agent/review.py samples/02_inefficient.sh --rewrite
python my-agent/review.py samples/03_pretty_good.js --rewrite -o report.md
```

| Option | Meaning |
|---|---|
| `--rewrite` | Also generate section 4 (rewrite + diff). This makes a second API call. |
| `-o FILE` | Write the markdown report to a file instead of the terminal. Without `-o`, after the report prints you're asked whether to export it as `.md` (default name `<file>_review.md`). The question is skipped when output is piped. |
| `-m NAME` | Gemini model. Defaults to `GEMINI_MODEL` from `.env`, then `gemini-3.5-flash`. |
| `--thinking LEVEL` | `minimal`, `low` (default), `medium` or `high`. Higher levels do more hidden reasoning, which can take minutes. |
| `--timeout N` | Per-request timeout in seconds. The default is 180. |

## How it works

- **Sections 1–3** come from a single call that uses Gemini's structured JSON output, defined by the `Review` schema in `gemini_client.py`. The schema includes an `is_source_code` flag, which is how non-code input gets caught.
- Line numbers are added to the source before it's sent, so the model can cite locations like `L12`.
- **Section 4** is a second call that returns only a fenced code block. The tool then runs `difflib` on it against the original to build the diff.
- API errors such as a bad key, an unknown model, rate limits or timeouts are shown as one-line messages, and the tool exits with code 1.
