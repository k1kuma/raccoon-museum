"""Optimal-lineup math for 'points left on bench'."""
from __future__ import annotations

BENCH_SLOTS = {"BN", "IR", "IR+", "NA"}
FLEX_LETTERS = {"Q": "QB", "W": "WR", "R": "RB", "T": "TE"}


def slot_allows(slot: str) -> set[str]:
    if "/" in slot:
        return {FLEX_LETTERS.get(p, p) for p in slot.split("/")}
    if slot in ("DEF", "D/ST", "DST"):
        return {"DEF"}
    return {slot}


def player_positions(eligible: str | None, primary: str | None) -> set[str]:
    positions = {p.strip() for p in (eligible or "").split(",") if p.strip()}
    if primary:
        positions.add(primary)
    if "D/ST" in positions or "DST" in positions:
        positions.add("DEF")
    return positions


def optimal_points(starter_slots: list[str], players: list[dict]) -> float:
    """Greedy fill, most-restrictive slots first. Exact for standard QB/RB/WR/TE/FLEX/K/DEF layouts.

    players: dicts with eligible_positions, primary_position, selected_position, points.
    """
    pool = [
        (p.get("points") or 0.0, player_positions(p.get("eligible_positions"), p.get("primary_position")))
        for p in players
        if p.get("selected_position") not in ("IR", "IR+", "NA")
    ]
    pool.sort(key=lambda x: -x[0])
    used = [False] * len(pool)
    total = 0.0
    for slot in sorted(starter_slots, key=lambda s: len(slot_allows(s))):
        allowed = slot_allows(slot)
        for i, (pts, pos) in enumerate(pool):
            if not used[i] and pos & allowed:
                used[i] = True
                total += pts
                break
    return round(total, 2)


def actual_points(players: list[dict]) -> float:
    return round(sum((p.get("points") or 0.0) for p in players if p.get("selected_position") not in BENCH_SLOTS), 2)
