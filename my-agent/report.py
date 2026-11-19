"""Turn a Review (+ optional rewrite) into a markdown report.

This module only formats text. It makes no API calls, which keeps it easy
to test and lets the CLI either render the result in the terminal (via
rich) or write it straight to a .md file.
"""

from __future__ import annotations

import difflib

from gemini_client import Review

# Map detected language names to markdown fence tags.
# Gemini reports languages as human-readable names ("C#", "Shell"), but
# markdown syntax highlighters expect specific tags ("csharp", "bash").
# Anything not listed here falls back to "text" (see fence_tag).
_FENCE_TAGS = {
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "bash": "bash", "shell": "bash", "c#": "csharp", "html": "html",
    "css": "css", "java": "java", "go": "go", "rust": "rust",
}


def fence_tag(language: str) -> str:
    """Return the markdown code-fence tag for a language name.

    Matching ignores case and surrounding whitespace. Unknown languages
    get "text" so the block still renders, just without highlighting.
    """
    return _FENCE_TAGS.get(language.strip().lower(), "text")


def _bullets(items: list[str], empty: str) -> str:
    """Render a list as markdown bullets, or show `empty` in italics if
    the list has no items (so a section never shows up blank)."""
    return "\n".join(f"- {item}" for item in items) if items else f"_{empty}_"


def build(filename: str, source: str, rv: Review, model: str,
          rewritten: str | None) -> str:
    """Assemble the full markdown report.

    Args:
        filename: Display name of the reviewed file (used in headings).
        source: Original file contents, used to diff against the rewrite.
        rv: Structured review returned by gemini_client.review().
        model: Model name, shown in the header for reference.
        rewritten: Rewritten file text, or None if --rewrite wasn't used
            (or the input wasn't source code).

    Returns:
        The report as one markdown string.
    """
    # The report is built as a list of lines and joined once at the end.
    # Empty strings ("") become blank lines between markdown blocks.

    # Header: always present, even for non-code input.
    out = [
        f"# Code review: `{filename}`",
        "",
        f"**Language:** {rv.language} | **Model:** {model}",
        "",
        f"> {rv.summary}",  # blockquote with the model's overall verdict
        "",
    ]

    # Prose or a made-up language: skip the review sections entirely and
    # return just the header plus a short notice.
    if not rv.is_source_code:
        out += [
            "**This input does not look like source code, so there is "
            "nothing to review or rewrite.**",
            "",
        ]
        return "\n".join(out)

    # Sections 1-3: the three required parts of the challenge.
    out += [
        "## 1. What was done well", "",
        _bullets(rv.done_well, "Nothing notable."), "",
        "## 2. What could be improved", "",
        _bullets(rv.improvements, "No improvements suggested."), "",
        "## 3. Bugs or syntax errors", "",
        _bullets(rv.bugs, "No bugs found."), "",
    ]

    # Section 4 (extra credit): the rewritten file plus a unified diff so
    # the reader can see exactly what changed.
    if rewritten is not None:
        tag = fence_tag(rv.language)
        # keepends=True keeps each line's "\n", which unified_diff needs
        # so its output lines join back together correctly.
        diff = "".join(difflib.unified_diff(
            source.splitlines(keepends=True),
            rewritten.splitlines(keepends=True),
            fromfile=f"{filename} (original)",
            tofile=f"{filename} (rewritten)",
        ))
        out += [
            "## 4. Rewritten version", "",
            f"```{tag}", rewritten.rstrip(), "```", "",
            "### Diff", "",
            # An empty diff means the model returned the file unchanged.
            "```diff", diff.rstrip() or "(no changes)", "```", "",
        ]

    return "\n".join(out)
