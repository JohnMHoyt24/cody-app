"""Thin wrapper around the Gemini API: config from .env, calls, error mapping.

Everything that talks to Google lives here. The rest of the agent only sees
three things: a `Review` object, a rewritten-file string, or an `AgentError`
with a readable message. SDK exceptions never leak out of this module.
"""

from __future__ import annotations

import os
import re

import httpx2 as httpx  # google-genai 3.x's HTTP transport
from dotenv import find_dotenv, load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from prompts import REVIEW_SYSTEM, REWRITE_SYSTEM

# Used when neither --model nor GEMINI_MODEL in .env picks a model.
DEFAULT_MODEL = "gemini-3.5-flash"


class AgentError(RuntimeError):
    """A user-facing failure; printed as one line by the CLI."""


class Review(BaseModel):
    """Structured result of a review call.

    This class does two jobs. It is passed to Gemini as `response_schema`,
    so the model is constrained to return JSON with exactly these fields.
    Pydantic then validates and parses that JSON back into this object.
    Field meanings are described to the model in prompts.REVIEW_SYSTEM.
    """
    is_source_code: bool    # False for prose / invented languages
    language: str           # e.g. "Python", or "none"
    summary: str            # one- or two-sentence overall verdict
    done_well: list[str]    # report section 1
    improvements: list[str]  # report section 2
    bugs: list[str]         # report section 3


def make_client(timeout_s: int) -> genai.Client:
    """Build a Gemini client using the API key from .env.

    Args:
        timeout_s: Per-request timeout in seconds (from --timeout).

    Raises:
        AgentError: If GEMINI_API_KEY is missing or blank.
    """
    # usecwd=True searches upward from the current working directory, not
    # from this file's location, so the repo-root .env is found when the
    # script runs from the repo root.
    load_dotenv(find_dotenv(usecwd=True))
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise AgentError(
            "GEMINI_API_KEY is not set. Add it to the .env file at the repo "
            "root (get a key at https://aistudio.google.com/apikey)."
        )
    return genai.Client(
        api_key=key,
        http_options=types.HttpOptions(
            timeout=timeout_s * 1000,  # the SDK expects milliseconds
            # Gemini often returns 429/503 under load; back off and retry.
            # 504 is NOT retried: it means our own deadline expired, and
            # retrying would just multiply an already-long wait.
            retry_options=types.HttpRetryOptions(
                attempts=3, initial_delay=2, max_delay=20,
                http_status_codes=[429, 500, 503],
            ),
        ),
    )


def resolve_model(cli_value: str | None) -> str:
    """Pick which model to use, in priority order:
    --model flag, then GEMINI_MODEL from .env, then DEFAULT_MODEL."""
    load_dotenv(find_dotenv(usecwd=True))
    return cli_value or os.getenv("GEMINI_MODEL") or DEFAULT_MODEL


# Valid values for --thinking. They're uppercased before being sent to the
# API (see _generate). review.py imports this tuple for argparse `choices`.
THINKING_LEVELS = ("minimal", "low", "medium", "high")


def _generate(client: genai.Client, model: str, system: str, user: str,
              thinking: str, **config) -> str:
    """Send one prompt to Gemini and return the response text.

    This is the single place that calls the API, so every SDK and network
    exception is turned into an AgentError here.

    Args:
        client: Client from make_client().
        model: Model name from resolve_model().
        system: System prompt (one of the constants in prompts.py).
        user: User message (the file contents plus its name).
        thinking: One of THINKING_LEVELS.
        **config: Extra GenerateContentConfig fields, e.g. the JSON
            response schema used by review().
    """
    # Default thinking on Gemini 3.x can burn minutes of hidden reasoning
    # (a 40-line rewrite took ~200s); "low" gives similar output in ~30s.
    try:
        resp = client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                # Low temperature keeps reviews consistent between runs.
                system_instruction=system, temperature=0.2,
                thinking_config=types.ThinkingConfig(
                    thinking_level=thinking.upper()),
                **config,
            ),
        )
    # 4xx errors: usually a problem on our side (key, model name, quota).
    # Map common codes to hints; fall back to the API's own message.
    except errors.ClientError as e:
        hints = {
            400: "bad request (is the API key valid?)",
            401: "the API key was rejected",
            403: "the API key was rejected or lacks access",
            404: f"model '{model}' unavailable: {e.message}",
            429: "rate limit or quota exceeded - wait a bit and retry",
        }
        raise AgentError(f"Gemini {e.code}: {hints.get(e.code, e.message)}") from e
    # 5xx errors that are still failing after the client's built-in retries.
    except errors.ServerError as e:
        raise AgentError(f"Gemini server error {e.code}: {e.message}") from e
    # TimeoutException is a subclass of HTTPError, so it has to be caught
    # first to get its more specific message.
    except httpx.TimeoutException as e:
        raise AgentError("Gemini request timed out (raise --timeout).") from e
    except httpx.HTTPError as e:
        raise AgentError(f"Network error talking to Gemini: {e}") from e

    # A request can succeed and still return no text, e.g. when a safety
    # filter blocks the prompt or the output. Report the reason Gemini
    # gives instead of passing an empty string along.
    text = resp.text
    if not text:
        reason = "unknown"
        if resp.candidates:
            reason = str(resp.candidates[0].finish_reason)
        elif resp.prompt_feedback:
            reason = str(resp.prompt_feedback.block_reason)
        raise AgentError(f"Gemini returned no text (reason: {reason}).")
    return text


def number_lines(source: str) -> str:
    """Prefix each line with a right-aligned, 1-based line number and " | ".

    Example: "   1 | import os". This lets the model cite exact line
    numbers ("L12") in the review; the prompt tells it the prefixes aren't
    part of the file.
    """
    return "\n".join(f"{i:4d} | {line}"
                     for i, line in enumerate(source.splitlines(), 1))


def review(client: genai.Client, model: str, filename: str,
           source: str, thinking: str = "low") -> Review:
    """Run the structured review (report sections 1-3).

    Gemini is asked for JSON constrained to the `Review` schema, and the
    reply is parsed into a `Review` object.

    Raises:
        AgentError: On any API failure, or if the JSON doesn't validate.
    """
    user = f"File name: {filename}\n\n{number_lines(source)}"
    raw = _generate(
        client, model, REVIEW_SYSTEM, user, thinking,
        # Structured output: the API enforces this JSON schema.
        response_mime_type="application/json",
        response_schema=Review,
    )
    # Even with a schema, validate before trusting the result. This catches
    # truncated or malformed responses.
    try:
        return Review.model_validate_json(raw)
    except ValidationError as e:
        raise AgentError(f"Gemini returned malformed JSON: {e}") from e


# Matches the first fenced code block and captures its body:
#   ```[^\n]*\n  opening fence plus optional language tag, through the newline
#   (.*?)        the code (non-greedy, so it stops at the first closing fence)
#   (?:\n```|\Z) the closing fence, or end of string if the reply was cut off
# DOTALL lets "." match newlines so the capture can span many lines.
_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:\n```|\Z)", re.DOTALL)


def rewrite(client: genai.Client, model: str, filename: str,
            source: str, thinking: str = "low") -> str:
    """Ask Gemini for an improved version of the file (report section 4).

    Returns only the code. Any commentary outside the code block is dropped,
    and the result always ends with exactly one newline.
    """
    # No line numbers here, or the model might copy them into its rewrite.
    user = f"File name: {filename}\n\n```\n{source}\n```"
    raw = _generate(client, model, REWRITE_SYSTEM, user, thinking)
    match = _FENCE.search(raw)
    # If the model skipped the fence, use the whole reply as the code.
    return (match.group(1) if match else raw).rstrip() + "\n"
