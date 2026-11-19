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

from engine.espn_feed import event_points, fetch_event, load_aliases, name_matches, norm
from engine.lineups import parse_lineup


def golfer_status(g, period: int | None, final: bool) -> str:
    """Short human status for the live panel: done | playing | yet | cut.

    done    - finished the round in progress (or the whole event is final)
    playing - mid-round
    yet     - hasn't teed off in the current round
    cut     - missed the cut / withdrew (<=2 rounds once round 3+ is underway)
    Only the live test on a real event can confirm these thresholds; they are
    deliberately simple.
    """
    if g is None:
        return "yet"
    if final:
        return "cut" if g.rounds_complete < 4 else "done"
    p = period or 1
    if g.partial:
        return "playing"
    if p >= 3 and g.rounds_complete <= 2:
        return "cut"
    return "done" if g.rounds_complete >= p else "yet"


def score_entrants(result: dict, entrants: list[dict], aliases: dict | None = None) -> dict:
    """entrants: [{"name","dk_handle","golfers":[6 names]}] -> scored + ranked.

    A golfer not found in the feed scores 0 and is listed in `unmatched` (this
    is correct for a did-not-start, but is surfaced so a real name-mismatch
    can't hide). Entrants without exactly 6 golfers are listed under `unranked`.
    """
    aliases = aliases if aliases is not None else load_aliases()
    pts = event_points(result)
    feed = {norm(n): n for n in pts}
    objs = {g.name: g for g in result["golfers"]}

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
                st = golfer_status(objs.get(feed[key]), result.get("period"), result["final"])
            golfer_fpts[g] = p
            detail.append({"golfer": g, "points": p, "status": st})
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


def build_status(result: dict, entrants: list[dict], week_key: str | None = None, meta: dict | None = None) -> dict:
    scored = score_entrants(result, entrants)
    return {
        **{k: (meta or {}).get(k) for k in META_KEYS},
        "week_key": week_key,
        "event": result["name"],
        "event_id": result["event_id"],
        "state": result["state"],
        "final": result["final"],
        "round_in_progress": result.get("period"),
        "detail": result.get("detail"),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "field_size": len(result["golfers"]),
        **scored,
    }


PUBLIC_FIELDS = ("week_key", "event", "event_id", "state", "final", "round_in_progress",
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
                   f"unmatched={len(status['unmatched'])} parse_errors={len(errs)}"
                   + (f" leader={top['name']} {top['points']}" if top else ""))
        print(f"ok: {result['name']} ({result['state']}), {len(status['entrants'])} entrants scored")
        return 0
    except Exception as e:  # loud, never silent; previous live_status.json is untouched
        log(a.log, f"FAILURE {type(e).__name__}: {e}")
        print(f"FAILURE {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
