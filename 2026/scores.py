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
    """Calculate team standings from live scores and draft picks."""
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

    # Calculate per-player and per-team scores
    team_results = {}
    players_counted = config["players_counted"]

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

                for rnd in ["round1", "round2", "round3", "round4"]:
                    score = get_round_total(api_player, rnd, pars.get(rnd, []))

                    # Apply missed cut penalty for rounds 3-4
                    if score is None and rnd in ["round3", "round4"] and not made_cut:
                        penalty = worst_cut_scores.get(rnd)
                        if penalty is not None:
                            score = penalty
                            rounds[rnd] = {"score": score, "penalty": True}
                        else:
                            # Cut scores not available yet — use cap
                            score = cut_max
                            rounds[rnd] = {"score": score, "penalty": True, "estimated": True}
                    elif score is not None:
                        rounds[rnd] = {"score": score, "penalty": False}
                        has_any_score = True
                    else:
                        rounds[rnd] = {"score": None, "penalty": False}

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

        # Sort by total (lowest first), None values at end
        player_scores.sort(key=lambda x: (x["total"] is None, x["total"] or 9999))

        # Best N players count
        counting = [p for p in player_scores if p["total"] is not None][:players_counted]
        dropped = [p for p in player_scores if p not in counting]

        team_total = sum(p["total"] for p in counting)

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
            "counting_players": [p["name"] for p in counting],
            "dropped_players": [p["name"] for p in dropped],
            "raw_total": team_total,
            "winner_bonus": winner_bonus,
            "drafted_winner": drafted_winner,
            "final_total": team_total + winner_bonus,
        }

    # Sort teams by final total
    standings = sorted(team_results.items(), key=lambda x: x[1]["final_total"])

    results = {
        "updated_at": datetime.now().isoformat(),
        "tournament_status": {
            "current_round": current_round,
            "status_round": status_round,
            "winner": winner,
            "worst_cut_scores": worst_cut_scores,
        },
        "standings": {drafter: data for drafter, data in standings},
    }

    return results, standings


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
    print()

    for rank, (drafter, data) in enumerate(standings, 1):
        bonus_str = f" (includes {data['winner_bonus']} winner bonus)" if data["winner_bonus"] else ""
        print(f"  {rank}. {drafter} — {data['final_total']} strokes{bonus_str}")
        if data["drafted_winner"]:
            print(f"     ** Drafted the champion: {data['drafted_winner']}! **")
        print()

        for p in data["players"]:
            counting = p["name"] in data["counting_players"]
            marker = " " if counting else "x"
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

            print(f"    [{marker}] {p['name']:<26} {str(r1):>3} {str(r2):>3} {str(r3)+r3_mark:>4} {str(r4)+r4_mark:>4}  = {total_str:>4}  {pos_str} {status_str}")

        print(f"    {'':>30} {'R1':>3} {'R2':>3} {'R3':>4} {'R4':>4}  {'TOT':>5}")
        print(f"    Counting: {', '.join(data['counting_players'])}")
        if data["dropped_players"]:
            print(f"    Dropped:  {', '.join(data['dropped_players'])}")
        print()

    print("  * = missed cut / WD penalty score")
    print("  x = dropped player (not counting toward total)")
    print("=" * 60)


def main():
    config = load_json(CONFIG_FILE)
    draft_state = load_json(DRAFT_STATE_FILE)

    if draft_state.get("status") != "complete":
        print("Warning: Draft is not yet complete. Scoring with current rosters.\n")

    print("Fetching live scores from masters.com...")
    scores_data = fetch_scores()

    results, standings = calculate_standings(scores_data, draft_state, config)

    # Save results
    save_json(RESULTS_FILE, results)
    print(f"Results saved to {RESULTS_FILE}\n")

    # Display
    display_standings(results, standings)


if __name__ == "__main__":
    main()
