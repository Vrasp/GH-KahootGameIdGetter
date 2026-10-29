
"""Inspect a live Kahoot session and look for its underlying quiz ID.

Usage:
    python get_game_id.py 345335

Requirements:
    pip install requests

The liveGameId returned by Kahoot is a session identifier and must not
be assumed to be the UUID of the underlying quiz.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any

import requests


SESSION_URL = "https://kahoot.it/reserve/session/{pin}/"

QUIZ_URLS = (
    "https://create.kahoot.it/rest/kahoots/{identifier}",
    "https://kahoot.it/rest/kahoots/{identifier}",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KahootSessionInspector/1.0)",
    "Accept": "application/json",
    "Origin": "https://kahoot.it",
    "Referer": "https://kahoot.it/",
}

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class LookupFailure(Exception):
    """Indicate that an active Kahoot game could not be inspected."""


def get_session(pin: str) -> tuple[dict[str, Any], bool]:
    """Retrieve session metadata from Kahoot using a live game PIN.

    The session token is never printed, decoded, or saved.

    Args:
        pin: The numeric game PIN.

    Returns:
        Tuple containing session metadata and token presence.

    Raises:
        LookupFailure: If the request or response is invalid.
    """
    try:
        response = requests.get(
            SESSION_URL.format(pin=pin),
            params={"_": int(time.time() * 1000)},
            headers=HEADERS,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise LookupFailure(
            f"Session request failed: {exc}"
        ) from exc

    if response.status_code == 404:
        raise LookupFailure(
            "No active Kahoot session was found for that PIN."
        )

    if response.status_code != 200:
        raise LookupFailure(
            f"Kahoot session API returned HTTP {response.status_code}."
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise LookupFailure(
            "Kahoot returned a non-JSON response."
        ) from exc

    if not isinstance(data, dict):
        raise LookupFailure(
            "Kahoot returned an unexpected response format."
        )

    has_token = bool(
        response.headers.get("X-Kahoot-Session-Token")
    )

    return data, has_token


def get_candidate_quiz_ids(
    data: dict[str, Any],
) -> list[tuple[str, str]]:
    """Find explicitly identified quiz UUIDs in session metadata.

    Does not assume liveGameId is a quiz UUID.

    Args:
        data: Kahoot session metadata.

    Returns:
        List of tuples containing candidate IDs and their source fields.
    """
    candidates = []

    sources = [("session", data)]

    nested = data.get("kahoot")

    if isinstance(nested, dict):
        sources.append(("session.kahoot", nested))

    for prefix, source in sources:
        for field in ("quizId", "kahootId", "uuid"):
            candidate = source.get(field)

            if not isinstance(candidate, str):
                continue

            if not UUID_RE.fullmatch(candidate):
                continue

            if any(
                identifier == candidate
                for identifier, _ in candidates
            ):
                continue

            candidates.append(
                (candidate, f"{prefix}.{field}")
            )

    return candidates


def lookup_quiz(
    identifier: str,
) -> tuple[str | None, str | None]:
    """Check whether an identifier resolves to a readable Kahoot quiz.

    Args:
        identifier: A possible Kahoot quiz or session identifier.

    Returns:
        A tuple of (verified quiz UUID, quiz title).
        Values are None if no readable quiz can be confirmed.
    """
    if not SAFE_ID_RE.fullmatch(identifier):
        print(
            "  Quiz lookup skipped: unsupported identifier characters."
        )
        return None, None

    for template in QUIZ_URLS:
        url = template.format(identifier=identifier)

        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=10,
            )
        except requests.RequestException as exc:
            print(
                f"  {url}: request failed ({type(exc).__name__})"
            )
            continue

        print(
            f"  {url}: HTTP {response.status_code}"
        )

        if response.status_code != 200:
            continue

        try:
            quiz = response.json()
        except ValueError:
            print(
                "  Response is not JSON; cannot verify a quiz."
            )
            continue

        if not isinstance(quiz, dict):
            continue

        title = quiz.get("title")

        if not isinstance(title, str):
            title = None

        uuid = quiz.get("uuid")

        # Verify that a successful quiz response includes
        # a properly formatted quiz UUID.
        if isinstance(uuid, str) and UUID_RE.fullmatch(uuid):
            return uuid, title

        print(
            "  Response did not contain a verified quiz UUID."
        )

    return None, None


def write_github_output(
    live_id: str | None,
    quiz_id: str | None,
) -> None:
    """Export discovered identifiers to GitHub Actions.

    Args:
        live_id: The live session identifier.
        quiz_id: The verified underlying quiz UUID, if found.
    """
    path = os.environ.get("GITHUB_OUTPUT")

    if not path:
        return

    with open(path, "a", encoding="utf-8") as output:
        output.write(
            f"game_id={live_id or ''}\n"
        )
        output.write(
            f"live_game_id={live_id or ''}\n"
        )
        output.write(
            f"quiz_id={quiz_id or ''}\n"
        )


def main() -> int:
    """Inspect a Kahoot session and report discovered identifiers.

    Returns:
        Zero for successful session lookup, one for an error.
    """
    if len(sys.argv) != 2:
        print(
            "Usage: python get_game_id.py <kahoot-pin>"
        )
        return 1

    pin = sys.argv[1].strip()

    if not re.fullmatch(r"\d{4,10}", pin):
        print(
            "[ERROR] Kahoot PIN must contain 4 to 10 digits."
        )
        return 1

    print(
        f"Looking up game info for PIN: {pin} ..."
    )

    try:
        data, has_token = get_session(pin)
    except LookupFailure as exc:
        print(f"[ERROR] {exc}")
        return 1

    print(
        f"Session token present: {has_token} "
        "(token not displayed)"
    )

    print(
        f"Response fields available: {list(data.keys())}"
    )

    raw_live_id = data.get("liveGameId")

    live_id = (
        str(raw_live_id)
        if raw_live_id is not None
        else None
    )

    print(
        f"\nLive game ID: {live_id or '(not provided)'}"
    )

    if live_id:
        print(
            f"Live game ID length: {len(live_id)} characters"
        )

        print(
            "Live game ID is a UUID: "
            f"{bool(UUID_RE.fullmatch(live_id))}"
        )

    candidates = get_candidate_quiz_ids(data)

    if candidates:
        for candidate, source in candidates:
            print(
                f"Potential quiz ID from {source}: {candidate}"
            )
    else:
        print(
            "No explicitly named quiz UUID "
            "was found in session metadata."
        )

    # Check explicitly named quiz IDs first.
    lookup_ids = [
        identifier
        for identifier, _ in candidates
    ]

    # Also test the liveGameId as a diagnostic.
    # Do not assume it identifies the underlying quiz.
    if live_id and live_id not in lookup_ids:
        lookup_ids.append(live_id)

    quiz_id = None
    quiz_title = None

    for identifier in lookup_ids:
        print(
            f"\nChecking quiz lookup for: {identifier}"
        )

        quiz_id, quiz_title = lookup_quiz(
            identifier
        )

        if quiz_id:
            break

    print("\n========== RESULTS ==========")
    print(f"Game PIN: {pin}")

    print(
        f"Live session ID: {live_id or 'Unavailable'}"
    )

    print(
        f"Verified quiz ID: {quiz_id or 'Not found'}"
    )

    if quiz_title:
        print(
            f"Quiz title: {quiz_title}"
        )

    if not quiz_id:
        print(
            "The live session was found, but its "
            "underlying quiz ID could not be verified."
        )

        print(
            "A liveGameId is not interchangeable "
            "with a quiz UUID."
        )

    print("=============================")

    write_github_output(
        live_id,
        quiz_id,
    )

    # A successful session lookup should not fail
    # merely because the quiz ID is unavailable.
    return 0


if __name__ == "__main__":
    sys.exit(main())
