"""
The score scraper: ESPN leaderboard -> DK points per golfer -> 3PC entrant
totals -> config/live_status.json.

    python -m engine.live_score --lineups config/current_week_lineups.json \
        --dates 20260219-20260222 [--event-id 4018...] [--out config/live_status.json]

No browser, no login, no Claude. Safe to run any time and as often as you like:
  * before the field posts / before round 1 -> writes state "pre", exits 0
  * mid-round -> per-hole points for holes played (no round bonuses until a
    round is complete, no placement bonus until the event is final)
  * final -> full DK points incl. placement bonus
Writes are atomic (temp file + rename): a crash or bad fetch never leaves a
corrupt live_status.json behind, and the previous good file is kept.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile

from engine.espn_feed import event_points, fetch_event, fetch_tee_times, load_aliases, name_matches, norm
from engine.lineups import parse_lineup

#: a tee time this far in the past with the golfer still not started is stale (a no-show or a withdrawal), not "upcoming"
TEE_STALE_AFTER = dt.timedelta(hours=6)


def _parse_tee(iso) -> dt.datetime | None:
    """ESPN writes tee times as UTC, e.g. "2026-11-20T14:14Z". None for anything unreadable."""
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def upcoming_tee(entry: dict | None, g, now: dt.datetime) -> dict | None:
    """The tee time to show beside a golfer: only for a round he has not started and that is not long past.

    entry is one value of espn_feed.parse_tee_times (None if ESPN lists no scheduled round); g is his GolferResult.
    Returns {"tee", "tee_hole", "tee_round"} or None."""
    if not entry or g is None or g.partial or entry["round"] <= g.rounds_complete:
        return None
    t = _parse_tee(entry["time"])
    if t is None or t < now - TEE_STALE_AFTER:
        return None
    return {"tee": entry["time"], "tee_hole": entry.get("hole"), "tee_round": entry["round"]}


def next_round_start(result: dict, now: dt.datetime) -> dict:
    """When the next round begins, for the header text "Round 1 Final · Round 2 starts 8:30 AM".

    Only between rounds (ESPN's "Play Complete" with the tournament still on): the earliest scheduled tee time of a
    later round across the whole field. Empty once anybody is on the course, so it can never contradict live play."""
    if not result.get("round_complete") or result.get("final") or any(g.partial for g in result["golfers"]):
        return {}
    period = result.get("period") or 0
    cands = []
    for e in (result.get("tee_times") or {}).values():
        t = _parse_tee(e["time"])
        if t is not None and e["round"] > period and t >= now - TEE_STALE_AFTER:
            cands.append((t, e["time"], e["round"]))
    if not cands:
        return {}
    _, raw, rnd = min(cands)
    return {"next_tee": raw, "next_tee_round": rnd}


def two_round_to_par(g) -> int | None:
    """Score to par after rounds 1 and 2 (what the cut is decided on); None until two rounds are complete."""
    if g.rounds_complete < 2:
        return None
    return sum(sum(r["scores"]) - sum(r["pars"]) for r in g.rounds[:2])


def find_cut_line(golfers) -> int | None:
    """The cut score (to par after 36 holes), worked out from who has teed off in Round 3: ESPN's feed has no cut flag.

    Round 3 tees off worst score first, so the golfers already out on the course are the ones at the cut line, and the
    worst 2-round total among them is the line (everyone tied at the line makes the cut). None until somebody has
    started Round 3, so nobody is called "cut" before it is known."""
    totals = [two_round_to_par(g) for g in golfers if g.rounds_complete >= 3 or (g.rounds_complete == 2 and g.partial)]
    totals = [t for t in totals if t is not None]
    return max(totals) if totals else None


def golfer_status(g, period: int | None, final: bool, cut_line: int | None = None) -> str:
    """Short human status for the live panel: done | playing | yet | cut | out.

    done    - finished the round in progress (or the whole event is final)
    playing - mid-round
    yet     - hasn't teed off in the current round
    cut     - missed the cut / withdrew. Never just because a round ended: the cut is made after Round 2, and
              before anyone has teed off in Round 3 (cut_line is None) nobody is called cut; after that a golfer with
              2 rounds is cut only if his 2-round score is worse than the line, otherwise he is just "yet" to tee off
    out     - not in the tournament field at all (withdrew before teeing off, or never entered): scores 0 in DraftKings
              and will never play, so he is not counted in "left"
    Only the live test on a real event can confirm these thresholds; they are
    deliberately simple.
    """
    if g is None:
        return "out"
    if final:
        return "cut" if g.rounds_complete < 4 else "done"
    p = period or 1
    if g.partial:
        return "playing"
    if p >= 3 and g.rounds_complete <= 2:
        if g.rounds_complete < 2 or p >= 4:
            return "cut"            # cannot be waiting for a tee time: withdrew, or missed the cut (Round 3 is over)
        if cut_line is None:
            return "done"           # Round 3 has not teed off yet: the cut line is not known, so nobody is called cut
        return "cut" if two_round_to_par(g) > cut_line else "yet"
    return "done" if g.rounds_complete >= p else "yet"


def score_entrants(result: dict, entrants: list[dict], aliases: dict | None = None, now: dt.datetime | None = None) -> dict:
    """entrants: [{"name","dk_handle","golfers":[6 names]}] -> scored + ranked.

    A golfer not found in the feed scores 0 and is listed in `unmatched` (this
    is correct for a did-not-start, but is surfaced so a real name-mismatch
    can't hide). Entrants without exactly 6 golfers are listed under `unranked`.
    """
    aliases = aliases if aliases is not None else load_aliases()
    pts = event_points(result)
    feed = {norm(n): n for n in pts}
    objs = {g.name: g for g in result["golfers"]}
    cut_line = find_cut_line(result["golfers"])
    now = now or dt.datetime.now(dt.timezone.utc)
    tee_by_norm = {norm(n): v for n, v in (result.get("tee_times") or {}).items()}

    scored, unranked, unmatched = [], [], []
    golfer_fpts: dict[str, float] = {}
    for e in entrants:
        golfers = e.get("golfers") or []
        if len(golfers) != 6:
            unranked.append({"name": e["name"], "reason": f"{len(golfers)} golfers parsed"})
            continue
        total, detail = 0.0, []
        for g in golfers:
            key = name_matches(g, feed, aliases)
            if key is None:
                unmatched.append({"entrant": e["name"], "golfer": g})
                p, st = 0.0, golfer_status(None, result.get("period"), result["final"])
            else:
                p = pts[feed[key]]
                st = golfer_status(objs.get(feed[key]), result.get("period"), result["final"], cut_line)
            golfer_fpts[g] = p
            # DraftKings exports turn accented letters into '?' ("Sami V?lim?ki"); when the golfer was matched to the
            # feed, show the feed's correctly spelled name instead. Scoring above still uses the DK spelling as given.
            shown = feed[key] if key is not None and "?" in g else g
            item = {"golfer": shown, "points": p, "status": st}
            if key is not None and not result["final"] and st not in ("cut", "out"):
                item.update(upcoming_tee(tee_by_norm.get(key), objs.get(feed[key]), now) or {})
            detail.append(item)
            total += p
        scored.append({"name": e["name"], "dk_handle": e.get("dk_handle"),
                       "points": total, "golfers": detail,
                       "to_play": sum(1 for d in detail if d["status"] in ("yet", "playing"))})

    for s in scored:  # ties share a place, like DK
        s["rank"] = 1 + sum(1 for o in scored if o["points"] > s["points"])
    scored.sort(key=lambda s: (s["rank"], s["name"]))
    return {"entrants": scored, "unranked": unranked, "unmatched": unmatched,
            "computed_golfer_fpts": golfer_fpts}


#: describes how the week counts in the 3PC standings (from the lineups file); the site uses it to project standings
META_KEYS = ("event_key", "point_type", "segment", "is_major")


def build_status(result: dict, entrants: list[dict], week_key: str | None = None, meta: dict | None = None,
                 now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    scored = score_entrants(result, entrants, now=now)
    return {
        **{k: (meta or {}).get(k) for k in META_KEYS},
        "week_key": week_key,
        "event": result["name"],
        "event_id": result["event_id"],
        "state": result["state"],
        "final": result["final"],
        "round_complete": bool(result.get("round_complete")),
        "round_in_progress": result.get("period"),
        "detail": result.get("detail"),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "field_size": len(result["golfers"]),
        "next_tee": None, "next_tee_round": None,       # filled between rounds only (see next_round_start)
        **next_round_start(result, now),
        **scored,
    }


PUBLIC_FIELDS = ("week_key", "event", "event_id", "state", "final", "round_complete", "round_in_progress",
                 "next_tee", "next_tee_round",
                 "detail", "updated_at", "field_size", "event_key", "point_type", "segment", "is_major")


def is_locked(locked_at: str | None, now: dt.datetime | None = None) -> bool:
    """True only when a valid lock time is set AND has passed. Anything missing,
    malformed or timezone-less counts as NOT locked (fail closed)."""
    if not locked_at:
        return False
    try:
        t = dt.datetime.fromisoformat(locked_at)
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    return now >= t


def public_view(status: dict, locked_at: str | None, now: dt.datetime | None = None) -> dict:
    """The ONLY shape that may be published to the public site.

    League rule: nobody may see anyone's lineup, golfer picks or any ownership
    information until lineups lock. Before lock this returns the event status
    with NO entrants, NO golfers and NO names of any kind. Fails closed: a
    missing/invalid/timezone-less lock time is treated as not yet locked.
    """
    out = {k: status.get(k) for k in PUBLIC_FIELDS}
    locked = is_locked(locked_at, now)
    out["locked"] = locked
    out["locked_at"] = locked_at
    if locked:
        out["entrants"] = status.get("entrants", [])
        out["unranked_count"] = len(status.get("unranked", []))
    return out


def atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def log(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} - {line}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lineups", required=True)
    ap.add_argument("--dates", help="ESPN dates, YYYYMMDD or YYYYMMDD-YYYYMMDD (default: 'espn_dates' in the lineups file)")
    ap.add_argument("--event-id", help="ESPN event id (default: 'espn_event_id' in the lineups file)")
    ap.add_argument("--out", default="config/live_status.json",
                    help="PRIVATE full status (never publish this)")
    ap.add_argument("--public-out", help="redacted file safe for the public site; "
                    "lineup data is included only after lineups lock (see public_view)")
    ap.add_argument("--log", default="logs/live_update.log")
    a = ap.parse_args(argv)

    try:
        with open(a.lineups, encoding="utf-8") as f:
            cfg = json.load(f)
        entrants = cfg["entrants"]
        dates = a.dates or cfg.get("espn_dates")
        if not dates:
            raise ValueError("no ESPN dates: pass --dates or set 'espn_dates' in the lineups file")
        result = fetch_event(dates, event_id=a.event_id or cfg.get("espn_event_id"))
        if result.get("team_event"):
            raise RuntimeError("team event (e.g. Zurich) - not supported by espn_feed yet")
        if result["state"] == "pre" or not result["golfers"]:
            log(a.log, f"{result['name']}: not started / field not posted yet - nothing to do")
            if a.public_out:  # publish the harmless "waiting" state (never any lineup data)
                waiting = {**{k: cfg.get(k) for k in META_KEYS}, "week_key": cfg.get("week_key"), "event": result["name"],
                           "event_id": result["event_id"], "state": "pre", "final": False,
                           "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
                atomic_write_json(a.public_out, public_view(waiting, cfg.get("locked_at")))
            print("not started")
            return 0
        if not result["final"]:
            result["tee_times"] = fetch_tee_times(result["event_id"])      # soft-fails to {}: tee times are optional
        status = build_status(result, entrants, cfg.get("week_key"), {k: cfg.get(k) for k in META_KEYS})
        errs = [f"{g.name}: {e}" for g in result["golfers"] for e in g.parse_errors]
        if errs:
            status["parse_errors"] = errs
        atomic_write_json(a.out, status)
        if a.public_out:
            atomic_write_json(a.public_out, public_view(status, cfg.get("locked_at")))
        top =status["entrants"][0] if status["entrants"] else None
        log(a.log, f"{result['name']} state={result['state']} round={result.get('period')} "
                   f"golfers={len(result['golfers'])} entrants={len(status['entrants'])} "
                   f"unmatched={len(status['unmatched'])} parse_errors={len(errs)} tee_times={len(result.get('tee_times') or {})}"
                   + (f" leader={top['name']} {top['points']}" if top else ""))
        print(f"ok: {result['name']} ({result['state']}), {len(status['entrants'])} entrants scored")
        return 0
    except Exception as e:  # loud, never silent; previous live_status.json is untouched
        log(a.log, f"FAILURE {type(e).__name__}: {e}")
        print(f"FAILURE {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
