"""
Fetches PGA Tour leaderboards from ESPN's public JSON feed and turns them
into per-golfer DK fantasy points via engine/dk_scoring.py.

No browser, no login, no Claude: a plain HTTPS GET. Validated 2026-11 against
real DK results (Tour Championship, Valero Texas Open) - see
backtest_2026.py for the full-season check.

Key rules baked in here (each confirmed against real weekly CSVs):
  * Hole par is derived as strokes - score_relative_to_par (feed gives both).
  * Tied finishers share a place: place = 1 + (# finishers strictly better).
  * A golfer "made the cut" iff they completed 4 rounds. Placement bonus only
    applies to them, and only once the event is final.
  * Rounds with < 18 holes (in progress / WD / DQ) still earn per-hole points
    but never earn round bonuses (bogey-free, streak) - see partial_round_points.
"""
from __future__ import annotations

import json
import os
import unicodedata
import urllib.request
from dataclasses import dataclass, field

from engine.dk_scoring import (
    ALL_ROUNDS_UNDER_70_BONUS,
    STREAK_BONUS,
    PLACEMENT_BONUS,
    hole_points,
    round_points,
    tournament_points,
)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard?dates={dates}"
ALIAS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "config", "golfer_aliases.json")


_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ł": "l", "Ł": "L",
                           "đ": "d", "Đ": "D", "ð": "d", "þ": "th", "ß": "ss"})


def norm(name: str) -> str:
    """Accent/punctuation/case-insensitive key for matching golfer names.

    NFKD alone drops letters like ø (Højgaard, Thorbjørn) instead of
    transliterating them, so those are mapped explicitly first.
    """
    s = unicodedata.normalize("NFKD", name.translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in s if ch.isalnum())


def name_matches(csv_name: str, feed_norm_names: dict[str, str], aliases: dict[str, str]) -> str | None:
    """Resolve a DK/CSV golfer name to a key in feed_norm_names.

    Handles: accents/case/punctuation (norm), the alias file, and CSV names where
    non-ASCII letters were corrupted to '?' (treated as a one-letter wildcard,
    accepted only when exactly one feed name fits).
    """
    n = norm(csv_name) if "?" not in csv_name else None
    if n:
        # aliases are directional (variant -> canonical) but the DK name may be
        # either side, so accept a match in both directions.
        if n in feed_norm_names:
            return n
        canon = aliases.get(n)
        if canon and canon in feed_norm_names:
            return canon
        return next((k for k in feed_norm_names if aliases.get(k) == n), None)
    import re
    pat = re.compile("^" + "".join("[a-z]" if ch == "?" else re.escape(ch)
                                   for ch in "".join(c for c in csv_name.lower() if c.isalnum() or c == "?")) + "$")
    hits = [k for k in feed_norm_names if pat.match(k)]
    return hits[0] if len(hits) == 1 else None


def load_aliases(path: str = ALIAS_PATH) -> dict[str, str]:
    """{norm(any variant): norm(canonical)} - maps feed/DK spelling variants together."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for canonical, variants in raw.items():
        if canonical.startswith("_"):
            continue
        for v in variants:
            out[norm(v)] = norm(canonical)
    return out


def _rel(display: str) -> int:
    d = display.strip()
    return 0 if d == "E" else int(d)


def _hole_par(strokes: int, display: str) -> int:
    """Par for one hole from strokes + the feed's score-vs-par label.

    The label is "E", "-1", "+2" ... but for extreme scores ESPN writes the
    word "OTHER" (e.g. an 11 at Shinnecock). DK pays a flat -1 for any hole
    double bogey or worse, so for OTHER we only need a par that makes the hole
    land in that bucket; a 1-2 stroke OTHER is treated as double-eagle-or-better.
    """
    d = display.strip()
    if d.upper() == "OTHER":
        return strokes - 3 if strokes >= 6 else strokes + 3
    return strokes - _rel(d)


@dataclass
class GolferResult:
    name: str
    rounds: list[dict] = field(default_factory=list)      # complete 18-hole rounds
    partial: dict | None = None                           # in-progress round: {"scores","pars"}
    playoff: dict | None = None                           # playoff holes (never scored by DK)
    to_par: int | None = None
    order: int | None = None
    parse_errors: list[str] = field(default_factory=list)  # loud, not silent

    @property
    def rounds_complete(self) -> int:
        return len(self.rounds)


def parse_competitor(c: dict) -> GolferResult:
    g = GolferResult(name=c["athlete"]["displayName"], order=c.get("order"))
    try:
        g.to_par = _rel(c.get("score") or "")
    except ValueError:
        g.to_par = None
    for r in c.get("linescores", []):
        holes = r.get("linescores", []) or []
        if not holes:
            continue
        period = r.get("period")
        try:
            scores = [int(h["value"]) for h in holes]
            pars = [_hole_par(int(h["value"]), h["scoreType"]["displayValue"]) for h in holes]
        except (KeyError, ValueError, TypeError) as e:
            g.parse_errors.append(f"round {period}: {type(e).__name__}: {e}")
            continue
        if len(holes) == 18 and (period or 0) <= 4:
            g.rounds.append({"scores": scores, "pars": pars})
        elif (period or 0) > 4:
            g.playoff = {"scores": scores, "pars": pars}
        else:
            g.partial = {"scores": scores, "pars": pars}
    # A 5th "round" of <18 holes after 4 complete rounds is a playoff even if
    # the feed numbers it oddly; never score it.
    if g.rounds_complete >= 4 and g.partial:
        g.playoff, g.partial = g.partial, None
    return g


def _is_final(ev_status: dict, comp_status: dict) -> bool:
    """True only when the TOURNAMENT is over.

    ESPN marks the competition "STATUS_PLAY_COMPLETE" with completed=True at the end of EVERY round ("Round 1 - Play
    Complete"), while the event itself stays "STATUS_IN_PROGRESS" until Sunday's finish (then both say STATUS_FINAL).
    Trusting the competition flag alone declared the RSM Classic final on Thursday night: no live placement bonus,
    "Final · unofficial" on the site, and the page stopped refreshing. The event-level status is the authority;
    if a payload has none, fall back to the competition but never count a between-rounds "Play Complete" as final."""
    if not comp_status.get("completed"):
        return False
    if ev_status:
        return bool(ev_status.get("completed"))
    return comp_status.get("name") != "STATUS_PLAY_COMPLETE"


def fetch_event(dates: str, event_id: str | None = None, timeout: int = 30) -> dict:
    """Returns {"event_id","name","final","golfers":[GolferResult]}.

    `dates` is ESPN's YYYYMMDD or YYYYMMDD-YYYYMMDD range. If several events
    fall in the range, pass event_id to pick one; otherwise the first is used.
    """
    data = json.load(urllib.request.urlopen(SCOREBOARD_URL.format(dates=dates), timeout=timeout))
    events = data.get("events", [])
    if not events:
        raise ValueError(f"no events for dates={dates}")
    ev = next((e for e in events if e["id"] == event_id), events[0]) if event_id else events[0]
    comp = ev["competitions"][0]
    comp.setdefault("competitors", [])  # before the field posts ESPN omits this key
    status = comp.get("status", {}).get("type", {})
    ev_status = ev.get("status", {}).get("type", {})
    return {
        "event_id": ev["id"],
        "name": ev["name"],
        "final": _is_final(ev_status, status),
        # the round just finished but the tournament has not ("Round 1 - Play Complete"): the page says "Round 1 Final"
        "round_complete": bool(status.get("completed")) and not _is_final(ev_status, status),
        # The EVENT's state, not the round's: between rounds the competition reads "post" but the tournament is still "in".
        "state": ev_status.get("state") or status.get("state"),   # pre | in | post
        "period": comp.get("status", {}).get("period"),     # round currently being played
        "detail": status.get("detail"),
        # Team events (Zurich) list teams, not athletes: not supported here yet.
        "team_event": any("athlete" not in c for c in comp["competitors"]),
        "golfers": [parse_competitor(c) for c in comp["competitors"] if "athlete" in c],
        "_all_events": [(e["id"], e["name"]) for e in events],
    }


LEADERBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/golf/leaderboard?league=pga&event={event_id}"


def parse_tee_times(data: dict) -> dict[str, dict]:
    """{golfer name: {"time": "2026-11-20T14:14Z", "hole": 10, "round": 2}} for every golfer ESPN lists as scheduled
    ("pre") for a round that has not teed off. Anything malformed is skipped, never guessed."""
    out: dict[str, dict] = {}
    for ev in data.get("events", [])[:1]:
        for comp in ev.get("competitions", [])[:1]:
            for c in comp.get("competitors", []):
                st = c.get("status") or {}
                name = (c.get("athlete") or {}).get("displayName")
                tee, rnd = st.get("teeTime"), st.get("period")
                if not name or not tee or not isinstance(rnd, int):
                    continue
                if (st.get("type") or {}).get("state") != "pre":
                    continue                    # already on the course, finished, or withdrawn
                hole = st.get("startHole")
                out[name] = {"time": tee, "hole": hole if isinstance(hole, int) else None, "round": rnd}
    return out


def parse_cut_names(data: dict) -> set[str]:
    """Normalized names of the golfers ESPN's leaderboard marks "Missed Cut" (status type STATUS_CUT).

    ESPN sets the flag for the whole field as soon as the cut is made (the moment Round 2 is complete), and a golfer who
    withdrew carries it too. Anything malformed is skipped, never guessed."""
    out: set[str] = set()
    for ev in data.get("events", [])[:1]:
        for comp in ev.get("competitions", [])[:1]:
            for c in comp.get("competitors", []):
                name = (c.get("athlete") or {}).get("displayName")
                if name and ((c.get("status") or {}).get("type") or {}).get("name") == "STATUS_CUT":
                    out.add(norm(name))
    return out


def fetch_leaderboard_extras(event_id: str, timeout: int = 15) -> tuple[dict[str, dict], set[str]]:
    """(tee times, missed-cut names) from one read of ESPN's leaderboard. Both are a nicety on top of scoring: any
    failure returns ({}, set()) so scoring and publishing carry on without them."""
    try:
        req = urllib.request.Request(LEADERBOARD_URL.format(event_id=event_id), headers={"User-Agent": "Mozilla/5.0"})
        data = json.load(urllib.request.urlopen(req, timeout=timeout))
        return parse_tee_times(data), parse_cut_names(data)
    except Exception:
        return {}, set()


def confirmed_cuts(result: dict, flagged: set[str]) -> set[str]:
    """ESPN's missed-cut flags, trusted only once the cut can exist: Round 2 is complete, or a later round is under way.
    Before that the set is empty, so nobody is called cut early."""
    p = result.get("period") or 0
    return set(flagged) if flagged and (p >= 3 or (p == 2 and result.get("round_complete"))) else set()


def assign_places(golfers: list[GolferResult]) -> dict[str, int]:
    """Tied-place for every golfer who completed 4 rounds.

    Ties share a place. The exception is a playoff: golfers who played extra
    holes are separated (winner 1st, runner-up 2nd, ...) using the feed's
    final `order`, because DK pays playoff losers 2nd, not a share of 1st.
    """
    finishers = [g for g in golfers if g.rounds_complete >= 4 and g.to_par is not None]
    places = {g.name: 1 + sum(1 for o in finishers if o.to_par < g.to_par) for g in finishers}
    by_score: dict[int, list[GolferResult]] = {}
    for g in finishers:
        by_score.setdefault(g.to_par, []).append(g)
    for group in by_score.values():
        if any(g.playoff for g in group):
            base = min(places[g.name] for g in group)
            ordered = sorted(group, key=lambda g: (g.order if g.order is not None else 10**6))
            for i, g in enumerate(ordered):
                places[g.name] = base + i
    return places


def partial_round_points(scores: list[int], pars: list[int]) -> float:
    """Points for an unfinished round: every hole played so far, plus the birdie-streak bonus the moment it's
    earned (DraftKings pays +3 for 3 or more birdies-or-better in a row as soon as it happens, max once a round).
    The bogey-free bonus can't be known until the 18th hole, so it is NOT included."""
    pts = sum(hole_points(s, p) for s, p in zip(scores, pars))
    run = best = 0
    for s, p in zip(scores, pars):
        run = run + 1 if s - p <= -1 else 0
        best = max(best, run)
    if best >= 3:
        pts += STREAK_BONUS
    return pts


def two_round_to_par(g: GolferResult) -> int | None:
    """Score to par after rounds 1 and 2 (what the cut is decided on); None until two rounds are complete."""
    if g.rounds_complete < 2:
        return None
    return sum(sum(r["scores"]) - sum(r["pars"]) for r in g.rounds[:2])


def find_cut_line(golfers: list[GolferResult]) -> int | None:
    """The cut score (to par after 36 holes), worked out from who has teed off in Round 3: ESPN's feed has no cut flag.

    Round 3 tees off worst score first, so the golfers already out on the course are the ones at the cut line, and the
    worst 2-round total among them is the line (everyone tied at the line makes the cut). None until somebody has
    started Round 3, so nobody is called "cut" before it is known."""
    totals = [two_round_to_par(g) for g in golfers if g.rounds_complete >= 3 or (g.rounds_complete == 2 and g.partial)]
    totals = [t for t in totals if t is not None]
    return max(totals) if totals else None


def is_cut(g: GolferResult, period: int | None, cut_line: int | None, cut_names: set[str] | None = None) -> bool:
    """True for a golfer with fewer than 3 rounds who is not going to play on: he withdrew, or missed the cut.

    ESPN's own "Missed Cut" flag (cut_names, see confirmed_cuts) decides it the moment the cut is made. Without the flag
    it is inferred once Round 3 is under way: his 2-round score is worse than the cut line. With the line still unknown
    (nobody has teed off in Round 3) nobody is cut, and a golfer who has made the cut but not yet teed off is NOT cut
    however late his tee time."""
    if cut_names and norm(g.name) in cut_names and g.rounds_complete <= 2 and not g.partial:
        return True
    p = period or 1
    if p < 3 or g.rounds_complete > 2 or g.partial:
        return False
    if g.rounds_complete < 2 or p >= 4:
        return True               # cannot be waiting for a tee time: withdrew, or missed the cut (Round 3 is over)
    return cut_line is not None and two_round_to_par(g) > cut_line


def live_places(golfers: list[GolferResult], period: int | None, cut_line: int | None = None,
                cut_names: set[str] | None = None) -> dict[str, int]:
    """Current leaderboard place for every golfer in the running, mid-event.

    DraftKings pays the placement bonus LIVE, on a golfer's current position (confirmed in the first live test:
    Max Greyserman leading at -4 showed 17.5 hole points + 30 for 1st = 47.5). Same tie rule as the final scoring:
    place = 1 + the number of golfers with a strictly better score-to-par, and everyone tied shares that place.
    Golfers who haven't teed off in Round 1, and golfers who have missed the cut or withdrawn (see is_cut), are not in
    the running. A golfer who made the cut but has not yet teed off in Round 3 still holds his place (the leaders tee
    off last, so dropping them would hand everyone ahead of the field a false first place).
    """
    running = [g for g in golfers if g.to_par is not None and (g.rounds_complete > 0 or g.partial)
               and not is_cut(g, period, cut_line, cut_names)]
    return {g.name: 1 + sum(1 for o in running if o.to_par < g.to_par) for g in running}


def golfer_points(g: GolferResult, place: int | None, final: bool) -> float:
    """DK points for one golfer. `place` is the final place when `final`, otherwise the CURRENT place (live)."""
    pts = sum(round_points(r["scores"], r["pars"]) for r in g.rounds)
    if g.partial:
        pts += partial_round_points(g.partial["scores"], g.partial["pars"])
    if final:
        if g.rounds_complete >= 4:
            if all(sum(r["scores"]) < 70 for r in g.rounds):
                pts += ALL_ROUNDS_UNDER_70_BONUS
            if place is not None:
                pts += PLACEMENT_BONUS.get(place, 0)
    elif place is not None:
        pts += PLACEMENT_BONUS.get(place, 0)        # live: bonus for where the golfer stands right now
    return pts


def event_places(result: dict) -> dict[str, int]:
    """{golfer_name: place} the scoring uses: final tied places once the event is over, live places until then.
    The site shows these same places beside each golfer, so the number you see is the one the points are built on."""
    if result["final"]:
        return assign_places(result["golfers"])
    return live_places(result["golfers"], result.get("period"), find_cut_line(result["golfers"]), result.get("cut_names"))


def event_points(result: dict) -> dict[str, float]:
    """{golfer_name: DK points} for everyone in the feed.

    Final events use the final tied places; events in progress use live places (see live_places), because
    that's what DraftKings shows on its leaderboard and what the 3PC projection should mirror."""
    places = event_places(result)
    return {g.name: golfer_points(g, places.get(g.name), result["final"]) for g in result["golfers"]}
