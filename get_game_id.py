"""
Kahoot Game ID Getter
Given a Kahoot PIN, retrieves the internal game/quiz UUID by querying
the Kahoot session API — the same endpoint a host's browser calls.
Also prints all available session info sorted and labelled.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import base64

import requests


KAHOOT_SESSION_URL = "https://kahoot.it/reserve/session/{}/?{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://kahoot.it",
    "Referer": "https://kahoot.it/",
}

UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Human-readable labels for well-known API fields
_FIELD_LABELS: dict[str, str] = {
    "live_game_id":          "Live Game ID",
    "pin":                   "Game PIN",
    "title":                 "Quiz Title",
    "description":           "Quiz Description",
    "quiz_type":             "Quiz Type",
    "language":              "Language",
    "creator":               "Creator",
    "creator_username":      "Creator Username",
    "number_of_questions":   "Number of Questions",
    "player_count":          "Current Player Count",
    "game_mode":             "Game Mode",
    "team_mode":             "Team Mode",
    "two_factor_auth":       "Two-Factor Auth",
    "show_nicknames":        "Show Nicknames",
    "cooperative":           "Cooperative Mode",
    "points_enabled":        "Points Enabled",
    "time_limit_enabled":    "Time Limit Enabled",
    "cover":                 "Cover Image",
    "visibility":            "Visibility",
    "audience":              "Audience",
    "difficulty":            "Difficulty",
    "tags":                  "Tags",
    "created":               "Created At",
    "modified":              "Last Modified",
    "status":                "Game Status",
}


def _try_decode_token(token: str) -> dict:
    """Attempt to base64-decode the session token and parse it as JSON."""
    for variant in (token, token + "=", token + "=="):
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                raw = decoder(variant)
                return json.loads(raw.decode("utf-8"))
            except Exception:
                continue
    return {}


def _solve_challenge(challenge: str) -> str | None:
    """
    Solve the JavaScript challenge returned by the Kahoot session API.

    The challenge is a JS snippet of the form:
        setTimeout(function(){
            var nonce = "...";
            var answer = nonce.split("").map(function(char, position){
                return String.fromCharCode(char.charCodeAt(0) + (position % N));
            }).join("");
            return answer;
        }, 0)

    We replicate the transformation in Python to produce the same answer.
    """
    nonce_match = re.search(r'var\s+\w+\s*=\s*["\']([^"\']+)["\']', challenge)
    if not nonce_match:
        return None
    nonce = nonce_match.group(1)

    mod_match = re.search(r"position\s*%\s*(\d+)", challenge)
    mod = int(mod_match.group(1)) if mod_match else 5

    return "".join(chr(ord(c) + (i % mod)) for i, c in enumerate(nonce))


def _xor_decode(token: str, challenge_answer: str) -> str:
    """XOR the raw session token with the challenge answer to get the WS token."""
    # Add padding incrementally until decoding succeeds
    decoded_token = b""
    for padding in ("", "=", "=="):
        try:
            decoded_token = base64.b64decode(token + padding)
            break
        except Exception:
            continue
    answer_bytes = challenge_answer.encode("utf-8")
    result = bytes(a ^ answer_bytes[i % len(answer_bytes)] for i, a in enumerate(decoded_token))
    return result.decode("utf-8", errors="replace")


def get_game_info(pin: str) -> tuple[str, dict]:
    """
    Retrieve the Kahoot game (quiz) UUID and full session data for the given PIN.

    Returns (game_id, data) where data is the parsed JSON from the API response,
    or raises SystemExit on failure.
    """
    timestamp = int(time.time() * 1000)
    url = KAHOOT_SESSION_URL.format(pin, timestamp)

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as exc:
        print(f"[ERROR] Request failed: {exc}")
        sys.exit(1)

    if response.status_code == 404:
        print(f"[ERROR] No active game found for PIN: {pin}")
        print("Make sure the game has been started by the host before running this tool.")
        sys.exit(1)

    if response.status_code != 200:
        print(f"[ERROR] Unexpected HTTP {response.status_code} from Kahoot API")
        print(response.text[:500])
        sys.exit(1)

    session_token = response.headers.get("X-Kahoot-Session-Token", "")

    try:
        data = response.json()
    except ValueError:
        print("[ERROR] Kahoot API returned non-JSON response")
        print(response.text[:500])
        sys.exit(1)

    game_id = None

    # ── Method 1: Look for game ID fields in the JSON body ────────────────
    # Check top-level fields first (modern Kahoot API returns liveGameId here)
    for field in ("liveGameId", "uuid", "quizId", "gameId", "kahootId"):
        candidate = str(data[field]) if data.get(field) else None
        if candidate and UUID_PATTERN.fullmatch(candidate):
            game_id = candidate
            break

    # Also check nested under "kahoot" key (older API format)
    if not game_id and "kahoot" in data and isinstance(data["kahoot"], dict):
        for raw in (
            data["kahoot"].get("uuid"),
            data["kahoot"].get("quizId"),
            data["kahoot"].get("gameId"),
        ):
            candidate = str(raw) if raw is not None else None
            if candidate and UUID_PATTERN.fullmatch(candidate):
                game_id = candidate
                break

    # ── Method 2: Decode the session token (base64 JSON) ─────────────────
    if not game_id and session_token:
        token_data = _try_decode_token(session_token)
        for raw in (
            token_data.get("uuid"),
            token_data.get("quizId"),
            token_data.get("gameId"),
            token_data.get("kahootId"),
        ):
            candidate = str(raw) if raw is not None else None
            if candidate and UUID_PATTERN.fullmatch(candidate):
                game_id = candidate
                break

    # ── Method 3: Solve challenge → XOR-decode token → scan for UUID ─────
    if not game_id and session_token and "challenge" in data:
        challenge_answer = _solve_challenge(data["challenge"])
        if challenge_answer:
            decoded = _xor_decode(session_token, challenge_answer)
            uuids = UUID_PATTERN.findall(decoded)
            if uuids:
                game_id = uuids[0]

    # ── Method 4: Regex scan the entire raw response ─────────────────────
    if not game_id:
        uuids = UUID_PATTERN.findall(response.text)
        if uuids:
            game_id = uuids[0]

    if not game_id:
        value = data.get("liveGameId")
        print(f"liveGameId value: {value!r}")
        print(f"liveGameId type: {type(value).__name__}")

        print("[ERROR] Could not extract the game ID from the Kahoot API response.")
        print(f"Response fields available: {list(data.keys())}")
        print(f"Session token present: {bool(session_token)}")
        sys.exit(1)

    return game_id, data


def _collect_session_info(pin: str, game_id: str, data: dict) -> dict[str, str]:
    """
    Harvest all interesting, human-readable fields from the API response dict.

    Returns an ordered dict mapping internal key → string value, with the
    live game id placed first.
    """
    info: dict[str, str] = {}

    # Always-present fields
    info["live_game_id"] = game_id
    info["pin"] = pin

    # Helper to safely pull a value and stringify it
    def _get(d: dict, *keys: str) -> str | None:
        for k in keys:
            v = d.get(k)
            if v is not None and v != "":
                return str(v)
        return None

    # Top-level fields
    for src_key, info_key in (
        ("status",              "status"),
        ("playerCount",         "player_count"),
        ("twoFactorAuth",       "two_factor_auth"),
    ):
        v = _get(data, src_key)
        if v is not None:
            info[info_key] = v

    # "kahoot" sub-object (quiz metadata)
    kahoot = data.get("kahoot") if isinstance(data.get("kahoot"), dict) else {}
    for src_key, info_key in (
        ("title",               "title"),
        ("description",         "description"),
        ("quizType",            "quiz_type"),
        ("type",                "quiz_type"),
        ("language",            "language"),
        ("creator",             "creator"),
        ("creatorUsername",     "creator_username"),
        ("numberOfQuestions",   "number_of_questions"),
        ("cover",               "cover"),
        ("visibility",          "visibility"),
        ("audience",            "audience"),
        ("difficulty",          "difficulty"),
        ("created",             "created"),
        ("modified",            "modified"),
    ):
        v = _get(kahoot, src_key)
        if v is not None and info_key not in info:
            info[info_key] = v

    # Also check top-level for the same quiz fields (some API versions return them there)
    for src_key, info_key in (
        ("title",               "title"),
        ("quizType",            "quiz_type"),
        ("type",                "quiz_type"),
        ("language",            "language"),
        ("numberOfQuestions",   "number_of_questions"),
        ("visibility",          "visibility"),
        ("audience",            "audience"),
        ("difficulty",          "difficulty"),
        ("created",             "created"),
        ("modified",            "modified"),
    ):
        v = _get(data, src_key)
        if v is not None and info_key not in info:
            info[info_key] = v

    # tags (list → comma-separated)
    raw_tags = kahoot.get("tags") or data.get("tags")
    if isinstance(raw_tags, list) and raw_tags:
        info["tags"] = ", ".join(str(t) for t in raw_tags)
    elif isinstance(raw_tags, str) and raw_tags:
        info["tags"] = raw_tags

    # gameOptions sub-object
    game_options = data.get("gameOptions")
    if not isinstance(game_options, dict):
        game_options = kahoot.get("gameOptions") or {}
    if isinstance(game_options, dict):
        for src_key, info_key in (
            ("isTeamGame",          "team_mode"),
            ("cooperative",         "cooperative"),
            ("showNicknames",       "show_nicknames"),
            ("pointsEnabled",       "points_enabled"),
            ("timeLimitEnabled",    "time_limit_enabled"),
            ("gameMode",            "game_mode"),
        ):
            v = _get(game_options, src_key)
            if v is not None:
                info[info_key] = v

    return info


def _print_session_info(info: dict[str, str]) -> None:
    """Print all session info sorted and labelled in a neat table."""
    # live_game_id and pin always come first; everything else is alphabetical
    priority = ["live_game_id", "pin"]
    ordered_keys = priority + sorted(k for k in info if k not in priority)

    col_width = max(len(_FIELD_LABELS.get(k, k)) for k in ordered_keys)

    print()
    print("=" * (col_width + 4 + 40))
    print("  Kahoot Session Info")
    print("=" * (col_width + 4 + 40))
    for key in ordered_keys:
        label = _FIELD_LABELS.get(key, key.replace("_", " ").title())
        value = info[key]
        print(f"  {label:<{col_width}}  {value}")
    print("=" * (col_width + 4 + 40))


def write_github_output(info: dict[str, str]) -> None:
    """Write all collected info as key=value pairs to $GITHUB_OUTPUT."""
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as fh:
            for key, value in info.items():
                fh.write(f"{key}={value}\n")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python get_game_id.py <kahoot-pin>")
        sys.exit(1)

    pin = sys.argv[1].strip()

    if not re.fullmatch(r"\d{4,10}", pin):
        print(f"[ERROR] '{pin}' does not look like a valid Kahoot PIN (4–10 digits).")
        sys.exit(1)

    print(f"Looking up game info for PIN: {pin} ...")
    game_id, data = get_game_info(pin)

    info = _collect_session_info(pin, game_id, data)
    _print_session_info(info)
    print(f"\n✅ Game ID: {game_id}")
    write_github_output(info)


if __name__ == "__main__":
    main()
