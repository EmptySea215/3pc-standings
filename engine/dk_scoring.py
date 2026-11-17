"""
Independent DraftKings PGA Classic fantasy-scoring formula.

Computes a golfer's DK fantasy points directly from hole-by-hole
(par, score) data and their final tournament placement - without ever
touching draftkings.com (which is permanently blocked to browser
automation; see project notes). Feeds the live-scoring pipeline: PGA
Tour's own leaderboard/scorecard pages give real hole-by-hole scoring
(not blocked), this module turns that into DK-equivalent points, and
engine/scoring.py turns THAT into 3PC league points the same way it
always has.

VALIDATION STATUS (2026-09-08): per-hole table + all three bonus rules
confirmed EXACT against real data - Scottie Scheffler's 2026 Tour
Championship (65-67-66-66, won at -16) computed to 127.5 DK FPTS,
matching the real value in your own 08302026_TourChamps.csv to the
decimal. That case exercised every bonus simultaneously (bogey-free
round, 3-birdie streak, all-4-rounds-under-70, 1st-place finish), so the
core formula is trustworthy.

NOT YET independently re-validated: the placement-bonus table below 1st
place. Values for 2nd through 50th are sourced from public DFS reference
sites (golfnewsnet, sportsdata.io) that agree with each other except at
the very top (double eagle / hole-in-one bonuses, which don't matter for
placement scoring). Cross-check opportunistically against real weekly
FPTS in the 3PC results CSVs as tournaments are processed - don't trust
blindly for a placement you haven't seen confirmed.
"""

HOLE_POINTS = {
    # score_relative_to_par: points. Anything <= -3 is double-eagle-or-better.
    -2: 8,      # eagle
    -1: 3,      # birdie
    0: 0.5,     # par
    1: -0.5,    # bogey
}
DOUBLE_EAGLE_OR_BETTER = 13
WORSE_THAN_DOUBLE_BOGEY = -1  # also covers exactly double bogey (+2)

STREAK_BONUS = 3          # 3+ birdies-or-better in a row, max 1 per round
BOGEY_FREE_BONUS = 3      # per round with no hole worse than par
ALL_ROUNDS_UNDER_70_BONUS = 5   # tournament-wide, all counted rounds < 70 strokes
HOLE_IN_ONE_BONUS = 5

# 1st confirmed exact via real-data validation above. Rest: unverified, see caveat.
PLACEMENT_BONUS = {
    1: 30, 2: 20, 3: 18, 4: 16, 5: 14, 6: 12, 7: 10, 8: 9, 9: 8, 10: 7,
    11: 6, 12: 6, 13: 6, 14: 6, 15: 6,
    16: 5, 17: 5, 18: 5, 19: 5, 20: 5,
    21: 4, 22: 4, 23: 4, 24: 4, 25: 4,
    26: 3, 27: 3, 28: 3, 29: 3, 30: 3,
    31: 2, 32: 2, 33: 2, 34: 2, 35: 2, 36: 2, 37: 2, 38: 2, 39: 2, 40: 2,
    **{p: 1 for p in range(41, 51)},
}


def hole_points(score: int, par: int, is_hole_in_one: bool = False) -> float:
    """Points for a single hole, given the actual stroke score and its par.

    A hole-in-one (score == 1, detected automatically) earns the normal
    score-vs-par points PLUS HOLE_IN_ONE_BONUS: par 3 ace = eagle 8 + 5 = 13,
    par 4 ace = double-eagle 13 + 5 = 18. Confirmed against Shane Lowry's
    2026 Masters (a par-3 ace: DK total exceeded the no-ace calc by exactly 5).
    """
    ace = is_hole_in_one or score == 1
    diff = score - par
    if diff <= -3:
        base = DOUBLE_EAGLE_OR_BETTER
    elif diff >= 2:
        base = WORSE_THAN_DOUBLE_BOGEY
    else:
        base = HOLE_POINTS[diff]
    return base + (HOLE_IN_ONE_BONUS if ace else 0)


def round_points(scores: list[int], pars: list[int]) -> float:
    """Points for one round (18 holes), including bogey-free and birdie-streak bonuses."""
    assert len(scores) == len(pars) == 18, "expected 18 holes"
    diffs = [s - p for s, p in zip(scores, pars)]
    pts = sum(hole_points(s, p) for s, p in zip(scores, pars))

    if all(d <= 0 for d in diffs):
        pts += BOGEY_FREE_BONUS

    max_streak = cur = 0
    for d in diffs:
        cur = cur + 1 if d <= -1 else 0
        max_streak = max(max_streak, cur)
    if max_streak >= 3:
        pts += STREAK_BONUS

    return pts


def tournament_points(rounds: list[dict], place: int | None, made_cut: bool = True) -> float:
    """
    rounds: list of {"scores": [18 ints], "pars": [18 ints]} for each round played.
    place: final tournament position (1-based). None if missed cut / withdrew.
    made_cut: DK awards no placement bonus to golfers who missed the cut,
        even if `place` is somehow set.

    Returns total DK fantasy points for the tournament.
    """
    pts = sum(round_points(r["scores"], r["pars"]) for r in rounds)

    if len(rounds) >= 4:
        strokes_by_round = [sum(r["scores"]) for r in rounds]
        if all(s < 70 for s in strokes_by_round):
            pts += ALL_ROUNDS_UNDER_70_BONUS

    if made_cut and place is not None:
        pts += PLACEMENT_BONUS.get(place, 0)

    return pts
