"""
Lineup text -> list of golfer names.

Lineups arrive in the DK export style ("G Tommy Fleetwood G Ryo Hisatsune ...")
or hand-typed with " | " or ", " separators (see web/extract_data.py for the
history of formats). Anything that can't be split with confidence is returned
as ONE string and flagged by the caller, never guessed at.
"""
import re

NOT_PLAYED = {"not submitted", "did not submit", "lineup not submitted",
              "did not qualify", "manual lineup", ""}


def parse_lineup(raw) -> tuple[list[str], bool]:
    """Returns (golfers, played). golfers has 6 names when parsed confidently."""
    if not raw or not isinstance(raw, str):
        return [], False
    text = raw.strip()
    if text.lower() in NOT_PLAYED:
        return [], False
    if re.match(r"^G\s", text) and text.count(" G ") >= 2:
        parts = [p.strip() for p in text.split(" G ") if p.strip()]
        return [p[2:].strip() if p.startswith("G ") else p for p in parts], True
    if " | " in text:
        return [p.strip() for p in text.split(" | ") if p.strip()], True
    if text.count(", ") >= 3:
        return [p.strip() for p in text.split(", ") if p.strip()], True
    return [text], True
