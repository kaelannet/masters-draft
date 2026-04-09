#!/usr/bin/env python3
"""
Masters Draft Scoring - Fetches live scores from masters.com and calculates
team standings based on draft picks and scoring rules.

Persists results to JSON for potential web app use.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "scoring_config.json")
DRAFT_STATE_FILE = os.path.join(SCRIPT_DIR, "draft_state.json")
RESULTS_FILE = os.path.join(SCRIPT_DIR, "results.json")

SCORES_URL = "https://www.masters.com/en_US/scores/feeds/2026/scores.json"

# Map draft names to API names where they differ
NAME_ALIASES = {
    "Ludvig Aberg": "Ludvig Åberg",
    "S.W. Kim": "Si Woo Kim",
}


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def fetch_scores():
    """Fetch live scoring data from masters.com."""
    req = urllib.request.Request(SCORES_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_round_total(player_data, round_key, pars):
    """Calculate a player's round total from hole-by-hole scores.

    Returns the round stroke total, or None if round not started.
    The API stores scores as strokes per hole (not relative to par).
    """
    round_info = player_data.get(round_key, {})

    # Check if round has been played
    if round_info.get("total") is not None:
        return round_info["total"]

    # Try summing hole-by-hole scores
    scores = round_info.get("scores", [])
    if all(s is None for s in scores):
        return None

    # Partial round — sum completed holes
    total = 0
    for s in scores:
        if s is not None:
            total += s
    return total


def get_round_topar(player_data, round_key, round_pars):
    """Calculate a player's score relative to par for a round.

    For completed rounds: score - 72 (or sum of pars).
    For partial rounds: strokes on played holes minus par on those holes.
    Returns None if round not started.
    """
    round_info = player_data.get(round_key, {})
    if not round_pars:
        round_pars = [4] * 18

    # Completed round
    if round_info.get("total") is not None:
        return round_info["total"] - sum(round_pars)

    scores = round_info.get("scores", [])
    if all(s is None for s in scores):
        return None

    # Partial round — compare only played holes to their par
    topar = 0
    for i, s in enumerate(scores):
        if s is not None:
            par_i = round_pars[i] if i < len(round_pars) else 4
            topar += s - par_i
    return topar


def player_made_cut(player_data):
    """Determine if a player made the cut."""
    status = player_data.get("status", "")
    # Masters statuses: "C" = made cut, "X" = hasn't started / pre-tournament,
    # "M" = missed cut, "W" = withdrawn, "D" = disqualified
    # If round3 has scores, they made the cut
    r3 = player_data.get("round3", {})
    r3_scores = r3.get("scores", [])
    if any(s is not None for s in r3_scores):
        return True
    if status == "M":
        return False
    # If cut hasn't happened yet, assume still in
    return True


def calculate_standings(scores_data, draft_state, config):
    """Calculate team standings from live scores and draft picks.

    Scoring is per-round: for each of the 4 rounds independently, take the
    best 6 of 8 player scores and sum them. The team raw_total is the sum
    of all completed round totals.
    """
    tournament = scores_data["data"]
    players_api = {p["full_name"]: p for p in tournament.get("player", [])}
    pars = tournament.get("pars", {})

    # Determine current state
    current_round = tournament.get("currentRound", "0")
    status_round = tournament.get("statusRound", "NNNN")

    # Build a name-matching lookup (handle slight name differences)
    def find_api_player(draft_name):
        """Match a drafted player name to the API data."""
        # Check aliases first
        aliased = NAME_ALIASES.get(draft_name, draft_name)
        if aliased in players_api:
            return players_api[aliased]
        # Exact match
        if draft_name in players_api:
            return players_api[draft_name]
        # Try last name match
        draft_last = draft_name.split()[-1].lower()
        draft_first = draft_name.split()[0].lower()
        for api_name, api_data in players_api.items():
            if (api_data["last_name"].lower() == draft_last and
                    api_data["first_name"].lower().startswith(draft_first[0])):
                return api_data
        # Fuzzy: last name only (risky but fallback)
        for api_name, api_data in players_api.items():
            if api_data["last_name"].lower() == draft_last:
                return api_data
        return None

    # Find the worst round scores among cut-makers for missed-cut penalty
    cut_max = config["missed_cut_rule"]["max_score_per_round"]
    worst_cut_scores = {"round3": None, "round4": None}

    for p in tournament.get("player", []):
        if not player_made_cut(p):
            continue
        for rnd in ["round3", "round4"]:
            total = get_round_total(p, rnd, pars.get(rnd, []))
            if total is not None:
                current_worst = worst_cut_scores[rnd]
                if current_worst is None or total > current_worst:
                    worst_cut_scores[rnd] = total

    # Cap at max
    for rnd in ["round3", "round4"]:
        if worst_cut_scores[rnd] is not None:
            worst_cut_scores[rnd] = min(worst_cut_scores[rnd], cut_max)

    # Find tournament winner
    winner = None
    for p in tournament.get("player", []):
        if p.get("pos") == "1" or p.get("pos") == "1T":
            winner = p["full_name"]
            break

    # Estimate cut line from all players in the field
    cut_line_estimate = None
    cut_official = False

    # Check if cut has happened (status_round chars 2+ are 'C' means cut applied)
    if len(status_round) >= 3 and status_round[2] == "C":
        cut_official = True

    # Estimate cut line from R1+R2 totals of all players
    field_r1r2 = []
    for p in tournament.get("player", []):
        r1 = get_round_total(p, "round1", pars.get("round1", []))
        r2 = get_round_total(p, "round2", pars.get("round2", []))
        if r1 is not None and r2 is not None:
            field_r1r2.append(r1 + r2)
        elif r1 is not None:
            field_r1r2.append(r1 * 2)  # extrapolate from R1

    if field_r1r2:
        field_r1r2.sort()
        # The player at approximately position 50 gives projected cut line
        cut_pos = min(49, len(field_r1r2) - 1)
        cut_line_estimate = field_r1r2[cut_pos]

    # Calculate per-player and per-team scores
    team_results = {}
    players_counted = config["players_counted"]
    round_keys = ["round1", "round2", "round3", "round4"]

    for drafter, roster in draft_state["teams"].items():
        player_scores = []

        for player_name in roster:
            api_player = find_api_player(player_name)
            rounds = {}
            total = 0
            has_any_score = False
            made_cut = True
            status = "pre-tournament"

            if api_player:
                made_cut = player_made_cut(api_player)
                raw_status = api_player.get("status", "X")
                if raw_status == "M":
                    status = "missed_cut"
                elif raw_status == "W":
                    status = "withdrawn"
                elif raw_status == "D":
                    status = "disqualified"
                elif raw_status == "C":
                    status = "active"
                else:
                    status = "pre-tournament"

                for rnd in round_keys:
                    round_pars = pars.get(rnd, [])
                    score = get_round_total(api_player, rnd, round_pars)
                    rnd_topar = get_round_topar(api_player, rnd, round_pars)

                    # Apply missed cut penalty for rounds 3-4
                    if score is None and rnd in ["round3", "round4"] and not made_cut:
                        penalty = worst_cut_scores.get(rnd)
                        if penalty is not None:
                            score = penalty
                            rnd_topar = penalty - sum(round_pars) if round_pars else penalty - 72
                            rounds[rnd] = {"score": score, "penalty": True, "topar": rnd_topar}
                        else:
                            # Cut scores not available yet — use cap
                            score = cut_max
                            rnd_topar = cut_max - sum(round_pars) if round_pars else cut_max - 72
                            rounds[rnd] = {"score": score, "penalty": True, "estimated": True, "topar": rnd_topar}
                    elif score is not None:
                        rounds[rnd] = {"score": score, "penalty": False, "topar": rnd_topar}
                        has_any_score = True
                    else:
                        rounds[rnd] = {"score": None, "penalty": False, "topar": None}

                    if score is not None:
                        total += score

            player_scores.append({
                "name": player_name,
                "rounds": rounds,
                "total": total if (has_any_score or not made_cut) else None,
                "made_cut": made_cut,
                "status": status,
                "position": api_player.get("pos", "") if api_player else "",
                "thru": api_player.get("thru", "") if api_player else "",
                "today": api_player.get("today", "") if api_player else "",
                "topar": api_player.get("topar", "") if api_player else "",
            })

        # --- Per-round counting: best 6 of 8 per round ---
        counting_per_round = {}
        round_totals = {}
        raw_total = 0

        for rnd in round_keys:
            # Collect (player_name, score) for this round
            round_scores = []
            for p in player_scores:
                rnd_info = p["rounds"].get(rnd, {})
                score = rnd_info.get("score")
                if score is not None:
                    round_scores.append((p["name"], score))

            if round_scores:
                # Sort ascending (best/lowest first), take best 6
                round_scores.sort(key=lambda x: x[1])
                best = round_scores[:players_counted]
                counting_per_round[rnd] = [name for name, _ in best]
                round_total = sum(score for _, score in best)
                round_totals[rnd] = round_total
                raw_total += round_total
            else:
                counting_per_round[rnd] = []
                round_totals[rnd] = None

        # Winner bonus
        winner_bonus = 0
        drafted_winner = None
        if winner:
            for p in player_scores:
                api_p = find_api_player(p["name"])
                if api_p and api_p["full_name"] == winner:
                    winner_bonus = config["bonuses"]["tournament_winner"]["value"]
                    drafted_winner = p["name"]
                    break

        team_results[drafter] = {
            "players": player_scores,
            "counting_per_round": counting_per_round,
            "round_totals": round_totals,
            "raw_total": raw_total,
            "winner_bonus": winner_bonus,
            "drafted_winner": drafted_winner,
            "final_total": raw_total + winner_bonus,
        }

    # --- Calculate forecast for each team ---
    for drafter, data in team_results.items():
        forecast = calculate_forecast(
            data, tournament, pars, find_api_player, config,
            cut_line_estimate, cut_official, worst_cut_scores, round_totals=data["round_totals"]
        )
        data["forecast"] = forecast

    # Sort teams by final total
    standings = sorted(team_results.items(), key=lambda x: x[1]["final_total"])

    results = {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "tournament_status": {
            "current_round": current_round,
            "status_round": status_round,
            "winner": winner,
            "worst_cut_scores": worst_cut_scores,
            "cut_line": cut_line_estimate,
            "cut_official": cut_official,
        },
        "standings": {drafter: data for drafter, data in standings},
    }

    return results, standings


def extrapolate_round(player_data, round_key, round_pars):
    """Extrapolate a partial round to a full 18-hole score.

    Uses the player's actual strokes on completed holes, then adds par
    for each remaining hole.  Returns (projected_18, holes_played) or
    (None, 0) if the round hasn't started.
    """
    round_info = player_data.get(round_key, {})

    # Finished round — return as-is
    if round_info.get("total") is not None:
        return round_info["total"], 18

    scores = round_info.get("scores", [])
    if not round_pars:
        round_pars = [4] * 18  # fallback to par-72

    played = 0
    strokes = 0
    for i, s in enumerate(scores):
        if s is not None:
            strokes += s
            played += 1

    if played == 0:
        return None, 0

    # Add par for each unplayed hole
    for i in range(18):
        par_i = round_pars[i] if i < len(round_pars) else 4
        if i >= len(scores) or scores[i] is None:
            strokes += par_i

    return strokes, played


def calculate_forecast(team_data, tournament, pars, find_api_player, config,
                       cut_line_estimate, cut_official, worst_cut_scores, round_totals):
    """Project a team's final total accounting for the cut.

    For each unplayed round, project all 8 players' scores, take best 6, sum.
    For in-progress rounds, extrapolate partial scores to 18 holes using par
    for unplayed holes.
    projected_total = actual round totals + projected round totals + winner_bonus.
    """
    round_keys = ["round1", "round2", "round3", "round4"]
    players_counted = config["players_counted"]
    cut_max = config["missed_cut_rule"]["max_score_per_round"]

    # Count completed rounds (those with a round total)
    completed_rounds = [rnd for rnd in round_keys if round_totals.get(rnd) is not None]
    remaining_rounds = [rnd for rnd in round_keys if round_totals.get(rnd) is None]
    rounds_remaining = len(remaining_rounds)

    if rounds_remaining == 0:
        return {
            "projected_total": team_data["final_total"],
            "rounds_remaining": 0,
            "projected_cuts": [],
            "cut_line_estimate": cut_line_estimate,
        }

    # Build extrapolated 18-hole projections for each player per round.
    # Always extrapolate from API hole-by-hole data when available — this
    # correctly projects partial rounds to 18 holes using par for unplayed
    # holes.  Falls back to the results.json score only if no API data.
    player_projected_rounds = {}
    any_player_has_score = False

    for p in team_data["players"]:
        api_player = find_api_player(p["name"])
        projected = {}
        for rnd in round_keys:
            if api_player:
                round_pars = pars.get(rnd, [])
                ext, holes = extrapolate_round(api_player, rnd, round_pars)
                if ext is not None:
                    projected[rnd] = ext
                    any_player_has_score = True
                else:
                    projected[rnd] = None
            else:
                # No API data — use score from results if available
                rnd_info = p["rounds"].get(rnd, {})
                score = rnd_info.get("score")
                if score is not None:
                    projected[rnd] = score
                    any_player_has_score = True
                else:
                    projected[rnd] = None

        player_projected_rounds[p["name"]] = projected

    if not any_player_has_score:
        return {
            "projected_total": None,
            "rounds_remaining": rounds_remaining,
            "projected_cuts": [],
            "cut_line_estimate": cut_line_estimate,
        }

    # Average projected full-round score per player (from all rounds with data)
    player_avgs = {}
    for p in team_data["players"]:
        proj = player_projected_rounds[p["name"]]
        scores = [v for v in proj.values() if v is not None]
        player_avgs[p["name"]] = sum(scores) / len(scores) if scores else 72

    # Determine which players are projected to miss the cut.
    # Only project a cut miss when the player has at least one full
    # completed round — partial-round extrapolations are too unreliable.
    projected_cuts = []
    for p in team_data["players"]:
        if p["status"] in ("missed_cut", "withdrawn", "disqualified"):
            projected_cuts.append(p["name"])
            continue

        if not cut_official and cut_line_estimate is not None:
            api_player = find_api_player(p["name"])
            if not api_player:
                continue

            # Check if player has at least one finished round
            r1_total = api_player.get("round1", {}).get("total")
            r2_total = api_player.get("round2", {}).get("total")

            if r1_total is not None and r2_total is not None:
                proj_36 = r1_total + r2_total
            elif r1_total is not None:
                proj_36 = r1_total * 2
            else:
                continue  # no completed round — assume make cut

            if proj_36 > cut_line_estimate:
                projected_cuts.append(p["name"])

    # Project ALL rounds uniformly — for each round, fill in missing players
    # with their average (or par), then take best 6.  This handles partially-
    # started rounds where only some players have teed off.
    projected_total_all_rounds = 0
    for rnd in round_keys:
        round_projections = []
        for p in team_data["players"]:
            proj = player_projected_rounds[p["name"]]
            extrapolated = proj.get(rnd)

            if extrapolated is not None:
                round_projections.append(extrapolated)
            elif p["name"] in projected_cuts and rnd in ("round3", "round4"):
                penalty = worst_cut_scores.get(rnd)
                round_projections.append(penalty if penalty is not None else cut_max)
            else:
                round_projections.append(player_avgs[p["name"]])

        round_projections.sort()
        best = round_projections[:players_counted]
        projected_total_all_rounds += sum(best)

    projected_total = round(projected_total_all_rounds + team_data["winner_bonus"])

    return {
        "projected_total": projected_total,
        "rounds_remaining": rounds_remaining,
        "projected_cuts": projected_cuts,
        "cut_line_estimate": cut_line_estimate,
    }


def display_standings(results, standings):
    """Pretty-print the current standings."""
    ts = results["tournament_status"]
    print("=" * 60)
    print("   MASTERS 2026 - DRAFT STANDINGS")
    print("=" * 60)
    print(f"  Updated: {results['updated_at'][:19]}")
    print(f"  Round status: {ts['status_round']}")
    if ts["winner"]:
        print(f"  Tournament winner: {ts['winner']}")
    if ts["worst_cut_scores"]["round3"]:
        print(f"  Missed cut penalty R3: {ts['worst_cut_scores']['round3']}  R4: {ts['worst_cut_scores']['round4']}")
    if ts.get("cut_line") is not None:
        official = " (official)" if ts.get("cut_official") else " (projected)"
        print(f"  Cut line: {ts['cut_line']}{official}")
    print()

    for rank, (drafter, data) in enumerate(standings, 1):
        bonus_str = f" (includes {data['winner_bonus']} winner bonus)" if data["winner_bonus"] else ""
        print(f"  {rank}. {drafter} — {data['final_total']} strokes{bonus_str}")
        if data["drafted_winner"]:
            print(f"     ** Drafted the champion: {data['drafted_winner']}! **")
        forecast = data.get("forecast", {})
        if forecast.get("rounds_remaining", 0) > 0 and forecast.get("projected_total") is not None:
            cut_note = ""
            if forecast.get("projected_cuts"):
                cut_note = f" ({len(forecast['projected_cuts'])} projected to miss cut)"
            print(f"     Projected: {forecast['projected_total']}{cut_note}")
        print()

        cpr = data.get("counting_per_round", {})
        for p in data["players"]:
            # Show per-round counting markers
            def rnd_marker(rnd):
                return " " if p["name"] in cpr.get(rnd, []) else "."

            r1 = p["rounds"].get("round1", {}).get("score", "-") or "-"
            r2 = p["rounds"].get("round2", {}).get("score", "-") or "-"
            r3_info = p["rounds"].get("round3", {})
            r4_info = p["rounds"].get("round4", {})
            r3 = r3_info.get("score", "-") or "-"
            r4 = r4_info.get("score", "-") or "-"
            r3_mark = "*" if r3_info.get("penalty") else ""
            r4_mark = "*" if r4_info.get("penalty") else ""

            total_str = str(p["total"]) if p["total"] is not None else "-"
            pos_str = f"({p['position']})" if p["position"] else ""
            status_str = ""
            if p["status"] == "missed_cut":
                status_str = " [MC]"
            elif p["status"] == "withdrawn":
                status_str = " [WD]"
            elif p["thru"] and p["thru"] != "F" and p["thru"] != "":
                status_str = f" thru {p['thru']}"

            print(f"    {p['name']:<26} {rnd_marker('round1')}{str(r1):>3} {rnd_marker('round2')}{str(r2):>3} {rnd_marker('round3')}{str(r3)+r3_mark:>4} {rnd_marker('round4')}{str(r4)+r4_mark:>4}  = {total_str:>4}  {pos_str} {status_str}")

        print(f"    {'':>30} {'R1':>3} {'R2':>3} {'R3':>4} {'R4':>4}  {'TOT':>5}")
        rt = data.get("round_totals", {})
        rt_strs = [str(rt.get(r)) if rt.get(r) is not None else "-" for r in ["round1", "round2", "round3", "round4"]]
        print(f"    Round totals (best 6):    {rt_strs[0]:>3} {rt_strs[1]:>3} {rt_strs[2]:>4} {rt_strs[3]:>4}  = {data['raw_total']:>4}")
        for rnd in ["round1", "round2", "round3", "round4"]:
            names = cpr.get(rnd, [])
            if names:
                print(f"    {rnd} counting: {', '.join(names)}")
        print()

    print("  * = missed cut / WD penalty score")
    print("  . = not counted this round (worst 2 dropped per round)")
    print("=" * 60)


def main():
    config = load_json(CONFIG_FILE)
    draft_state = load_json(DRAFT_STATE_FILE)

    if draft_state.get("status") != "complete":
        print("Warning: Draft is not yet complete. Scoring with current rosters.\n")

    print("Fetching live scores from masters.com...")
    scores_data = fetch_scores()

    results, standings = calculate_standings(scores_data, draft_state, config)

    # Compare forecasts to previous run for trend indicators
    try:
        prev_results = load_json(RESULTS_FILE)
        prev_standings = prev_results.get("standings", {})
    except (FileNotFoundError, json.JSONDecodeError):
        prev_standings = {}

    TREND_THRESHOLD = 3  # strokes — ignore changes smaller than this
    for drafter, data in results["standings"].items():
        forecast = data.get("forecast", {})
        prev_data = prev_standings.get(drafter, {})
        prev_forecast = prev_data.get("forecast", {})
        prev_proj = prev_forecast.get("projected_total")
        curr_proj = forecast.get("projected_total")

        if prev_proj is not None and curr_proj is not None:
            delta = curr_proj - prev_proj
            if delta <= -TREND_THRESHOLD:
                forecast["trend"] = "improving"
                forecast["trend_delta"] = delta
            elif delta >= TREND_THRESHOLD:
                forecast["trend"] = "worsening"
                forecast["trend_delta"] = delta
            else:
                forecast["trend"] = "steady"
                forecast["trend_delta"] = delta
        else:
            forecast["trend"] = None
            forecast["trend_delta"] = None

    # Save results
    save_json(RESULTS_FILE, results)
    print(f"Results saved to {RESULTS_FILE}\n")

    # Display
    display_standings(results, standings)


if __name__ == "__main__":
    main()
