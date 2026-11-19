#!/usr/bin/env python3
"""AI code-review agent powered by Google Gemini.

    python my-agent/review.py FILE [--rewrite] [-o REPORT.md] [--model NAME]

Reads GEMINI_API_KEY (and optionally GEMINI_MODEL) from the repo's .env.

This is the CLI entry point. It parses arguments, reads the input file,
calls the Gemini wrapper (gemini_client.py), builds the markdown report
(report.py), and then prints the report or saves it to a file.
"""

# Lets annotations like `list[str] | None` work as plain strings, so they
# don't need to be evaluated at import time.
from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt

import report
from gemini_client import (THINKING_LEVELS, AgentError, make_client,
                           resolve_model, review, rewrite)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Define and parse the command-line options.

    Args:
        argv: Arguments to parse, or None to use sys.argv (normal CLI use).
            Passing a list makes the function easy to call from tests.
    """
    p = argparse.ArgumentParser(
        prog="review.py",
        description="Review a single source file with Google Gemini.",
    )
    # Required positional argument: the file to review.
    p.add_argument("file", type=Path, help="source file to review")
    # Optional extra-credit section 4: makes a second API call.
    p.add_argument("--rewrite", action="store_true",
                   help="also produce a rewritten file + diff (section 4)")
    # With -o, save the report directly and skip the terminal output.
    p.add_argument("-o", "--output", type=Path,
                   help="write the markdown report here instead of the terminal")
    # If left out, resolve_model() falls back to .env and then the default.
    p.add_argument("-m", "--model",
                   help="Gemini model (default: $GEMINI_MODEL or gemini-3.5-flash)")
    # argparse rejects any value not listed in THINKING_LEVELS.
    p.add_argument("--thinking", choices=THINKING_LEVELS, default="low",
                   help="how much hidden reasoning Gemini does; higher is "
                        "slower (default: low)")
    p.add_argument("--timeout", type=int, default=180,
                   help="per-request timeout in seconds (default: 180)")
    return p.parse_args(argv)


@contextmanager
def working(console: Console, message: str):
    """Spinner with a live elapsed-seconds counter, so a slow call is
    visibly still working rather than looking hung.

    Usage:
        with working(console, "Reviewing..."):
            do_slow_thing()

    rich's spinner animates on its own, but its text stays fixed. A
    background thread updates the text once a second with the elapsed time
    while the main thread waits on the network call.
    """
    # monotonic() never jumps backward (unlike time.time()), so it's the
    # right clock for measuring durations.
    start = time.monotonic()
    with console.status(message) as st:
        stop = threading.Event()

        def tick() -> None:
            # wait(1) returns False on a timeout (keep looping) and True
            # once stop.set() is called (exit the loop).
            while not stop.wait(1):
                st.update(f"{message} [dim]{time.monotonic() - start:.0f}s[/dim]")

        # daemon=True: the thread can't keep the process alive by itself
        # if something goes badly wrong.
        t = threading.Thread(target=tick, daemon=True)
        t.start()
        try:
            yield  # the body of the caller's `with` block runs here
        finally:
            # Always stop the ticker, even if the API call raised.
            stop.set()
            t.join()
    # Once the spinner is cleared, leave a permanent line with the timing.
    console.print(f"[dim]{message.rstrip('.')} done in "
                  f"{time.monotonic() - start:.0f}s[/dim]")


def run(args: argparse.Namespace) -> None:
    """Do one review from start to finish using the parsed arguments.

    Raises:
        AgentError: For any expected failure (bad input file, missing API
            key, API error). main() turns it into a one-line message.
    """
    # Validate the input before spending an API call on it.
    if not args.file.is_file():
        raise AgentError(f"not a file: {args.file}")
    # errors="replace" swaps undecodable bytes for "?" instead of crashing,
    # so a file with odd encoding can still be reviewed.
    source = args.file.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        raise AgentError(f"{args.file} is empty.")

    client = make_client(args.timeout)
    model = resolve_model(args.model)
    # Status messages go to stderr so stdout carries only the report,
    # which keeps `review.py file.py > out.txt` clean.
    status = Console(stderr=True)

    # Call 1: the structured review (sections 1-3).
    with working(status, f"Reviewing {args.file.name} with {model}..."):
        rv = review(client, model, args.file.name, source, args.thinking)

    # Call 2 (optional): the rewrite. Skipped for non-code input, since
    # rewriting an English paragraph wouldn't make sense.
    rewritten = None
    if args.rewrite and rv.is_source_code:
        with working(status, "Writing an improved version..."):
            rewritten = rewrite(client, model, args.file.name, source,
                                args.thinking)

    md = report.build(args.file.name, source, rv, model, rewritten)

    if args.output:
        # Non-interactive path: write the file and stop.
        args.output.write_text(md, encoding="utf-8")
        status.print(f"[green]Report written to {args.output}[/green]")
    else:
        # Interactive path: render the markdown in the terminal with
        # syntax-highlighted code blocks, then offer to save it.
        Console().print(Markdown(md, code_theme="monokai"))
        offer_export(md, args.file, status)


def offer_export(md: str, source: Path, console: Console) -> None:
    """After printing to the terminal, offer to save the report as markdown.

    Only asks when both stdin and stdout are interactive, so piped or
    scripted runs never block waiting for an answer.

    Args:
        md: The report's markdown text.
        source: Path of the reviewed file; its name becomes the default
            report name (e.g. foo.py -> foo_review.md).
        console: Console used for the prompts and status messages.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return

    # First stage: ask the questions. Ctrl+C or Ctrl+D (Ctrl+Z on Windows)
    # at any prompt cancels quietly instead of printing a traceback.
    try:
        console.print()
        if not Confirm.ask("Export this report as a .md file?",
                           console=console, default=False):
            return
        default_name = f"{source.stem}_review.md"
        name = Prompt.ask("File name", console=console, default=default_name)

        folder = Prompt.ask("Where to save it? (press Enter for current directory)",
                            console=console, default=".")

        # Strip quotes so a pasted Windows "Copy as path" value still works.
        # expanduser() turns a leading "~" into the home directory.
        folder_path = Path(folder.strip().strip('"\'') or ".").expanduser()
        out = folder_path / (name.strip() or default_name)
        # Make sure the file ends in .md. The suffix is appended rather than
        # replaced, so "notes.txt" becomes "notes.txt.md" and nothing the
        # user typed gets dropped.
        if out.suffix.lower() != ".md":
            out = out.with_suffix(out.suffix + ".md")
        # Never overwrite an existing file without asking first.
        if out.exists() and not Confirm.ask(
                f"{out} already exists. Overwrite?", console=console,
                default=False):
            console.print("[yellow]Not saved.[/yellow]")
            return
    except (EOFError, KeyboardInterrupt):
        console.print()  # move off the half-finished prompt line
        return

    # Second stage: write the file. Create any missing folders, and report
    # filesystem errors (bad path, no permission) without crashing.
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    except OSError as e:
        console.print(f"[red]Could not save report: {e}[/red]")
        return
    # resolve() shows the full absolute path, so it's clear where the file went.
    console.print(f"[green]Report written to {out.resolve()}[/green]")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code.

    0 = success, 1 = handled error (AgentError), 130 = interrupted with
    Ctrl+C (the usual shell convention: 128 + SIGINT).
    """
    args = parse_args(argv)
    # Windows consoles default to cp1252, which can't encode rich's glyphs.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        run(args)
    except AgentError as e:
        # Expected failures get a short message, not a stack trace.
        print(f"review: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


# Runs only when the file is executed directly, not when it's imported.
if __name__ == "__main__":
    sys.exit(main())
