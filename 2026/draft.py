#!/usr/bin/env python3
"""
Masters Draft CLI - Snake draft for 3 players.
Persists all state to JSON so it can be resumed or used in a web app later.
"""

import json
import random
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLAYERS_FILE = os.path.join(SCRIPT_DIR, "players_2026.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "scoring_config.json")
DRAFT_STATE_FILE = os.path.join(SCRIPT_DIR, "draft_state.json")


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_rankings():
    """Build a name -> world_ranking lookup from the players file."""
    players_data = load_json(PLAYERS_FILE)
    return {p["name"]: p.get("world_ranking") for p in players_data["players"]}


def rank_sort_key(name, rankings):
    """Sort key: ranked players first (by rank), then unranked (alphabetical)."""
    rank = rankings.get(name)
    if rank is not None:
        return (0, rank, name)
    return (1, 9999, name)


def build_snake_order(drafters, rounds):
    """Generate snake draft order: 1-2-3, 3-2-1, 1-2-3, ..."""
    order = []
    for r in range(rounds):
        round_order = list(drafters) if r % 2 == 0 else list(reversed(drafters))
        order.extend(round_order)
    return order


def format_rank(rank):
    """Format a world ranking for display."""
    if rank is not None:
        return f"#{rank:<4}"
    return "  -- "


def display_available(players, rankings, columns=2):
    """Display available players sorted by world ranking in columns."""
    sorted_players = sorted(players, key=lambda n: rank_sort_key(n, rankings))
    col_width = 42
    rows = (len(sorted_players) + columns - 1) // columns
    print("\n--- Available Players (sorted by OWGR) ---")
    for row in range(rows):
        parts = []
        for col in range(columns):
            idx = col * rows + row
            if idx < len(sorted_players):
                name = sorted_players[idx]
                rank = format_rank(rankings.get(name))
                parts.append(f"  {idx + 1:>2}. {rank} {name:<{col_width - 8}}")
        print("".join(parts))
    print()


def display_picked(picks, teams, rankings):
    """Display all picked players with drafter and ranking info."""
    print("\n===== Drafted Players =====")
    print(f"  {'Pick':>4}  {'OWGR':>5}  {'Player':<28} {'Drafted by'}")
    print(f"  {'----':>4}  {'-----':>5}  {'----------------------------':<28} {'----------'}")
    for pick in picks:
        rank = rankings.get(pick["player"])
        rank_str = f"#{rank}" if rank else "--"
        print(f"  {pick['pick_number']:>4}  {rank_str:>5}  {pick['player']:<28} {pick['drafter']}")
    print()
    print("  Team rosters:")
    for drafter, roster in teams.items():
        sorted_roster = sorted(roster, key=lambda n: rank_sort_key(n, rankings))
        roster_str = []
        for name in sorted_roster:
            rank = rankings.get(name)
            tag = f"(#{rank})" if rank else "(--)"
            roster_str.append(f"{name} {tag}")
        print(f"    {drafter}: {', '.join(roster_str) if roster_str else '(no picks yet)'}")
    print("===========================\n")


def display_teams(teams, rankings=None):
    """Display current team rosters."""
    print("\n===== Current Teams =====")
    for drafter, picks in teams.items():
        if picks:
            if rankings:
                sorted_picks = sorted(picks, key=lambda n: rank_sort_key(n, rankings))
                pick_strs = []
                for name in sorted_picks:
                    rank = rankings.get(name)
                    tag = f"(#{rank})" if rank else "(--)"
                    pick_strs.append(f"{name} {tag}")
                pick_str = ", ".join(pick_strs)
            else:
                pick_str = ", ".join(picks)
        else:
            pick_str = "(no picks yet)"
        print(f"  {drafter}: {pick_str}")
    print("=========================\n")


def fuzzy_match(query, players, rankings):
    """Find the best match for a player query. Players sorted by rank for display."""
    query_lower = query.strip().lower()

    # Try exact match first
    for p in players:
        if p.lower() == query_lower:
            return p

    # Try starts-with on last name
    for p in players:
        parts = p.lower().split()
        if any(part.startswith(query_lower) for part in parts):
            return p

    # Try substring
    matches = [p for p in players if query_lower in p.lower()]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        # Sort disambiguation list by ranking
        matches.sort(key=lambda n: rank_sort_key(n, rankings))
        return matches  # Return list for disambiguation

    return None


def init_draft():
    """Initialize or resume a draft."""
    config = load_json(CONFIG_FILE)
    players_data = load_json(PLAYERS_FILE)
    rankings = load_rankings()
    all_players = [p["name"] for p in players_data["players"]]

    # Check for existing draft state
    if os.path.exists(DRAFT_STATE_FILE):
        state = load_json(DRAFT_STATE_FILE)
        if state.get("status") == "complete":
            print("\nA completed draft already exists!")
            display_teams(state["teams"], rankings)
            resp = input("Start a new draft? (y/n): ").strip().lower()
            if resp != "y":
                return None
        else:
            print("\nFound an in-progress draft!")
            display_teams(state["teams"], rankings)
            resp = input("Resume this draft? (y/n): ").strip().lower()
            if resp == "y":
                return state

    # New draft
    drafters = list(config["drafters"])
    random.shuffle(drafters)
    print(f"\nRandomized draft order: {' -> '.join(drafters)}")

    draft_order = build_snake_order(drafters, config["draft_rounds"])
    teams = {d: [] for d in drafters}

    state = {
        "status": "in_progress",
        "created_at": datetime.now().isoformat(),
        "draft_order": draft_order,
        "drafters": drafters,
        "teams": teams,
        "picks": [],
        "current_pick": 0,
        "available_players": sorted(all_players),
    }
    save_json(DRAFT_STATE_FILE, state)
    return state


def print_help():
    print("\n  Commands:")
    print("    <number>    - Pick player by list number")
    print("    <name>      - Pick player by name (fuzzy match)")
    print("    list        - Show available players sorted by world ranking")
    print("    picked      - Show all drafted players and team rosters")
    print("    teams       - Show current team rosters")
    print("    help        - Show this help message")
    print("    quit        - Save and exit (resume later)")
    print()


def run_draft():
    """Main draft loop."""
    print("=" * 50)
    print("   MASTERS 2026 - SNAKE DRAFT")
    print("=" * 50)

    state = init_draft()
    if state is None:
        return

    config = load_json(CONFIG_FILE)
    rankings = load_rankings()
    total_picks = len(state["draft_order"])
    num_drafters = len(state["drafters"])

    print(f"\nFormat: Snake draft, {config['draft_rounds']} rounds, {num_drafters} drafters")
    print(f"Each team drafts {config['players_per_team']} players, best {config['players_counted']} count")
    print(f"Winner bonus: {config['bonuses']['tournament_winner']['value']} strokes")
    print(f"\nDraft order (round 1): {' -> '.join(state['drafters'])}")
    print(f"Snake reverses each round.")
    print("Type 'help' for commands.\n")

    while state["current_pick"] < total_picks:
        pick_num = state["current_pick"]
        current_round = pick_num // num_drafters + 1
        pick_in_round = pick_num % num_drafters + 1
        drafter = state["draft_order"][pick_num]

        print(f"--- Round {current_round}, Pick {pick_in_round} ---")
        print(f"{drafter}'s turn to pick ({len(state['teams'][drafter])}/{config['players_per_team']} players)")

        available = state["available_players"]
        # Sort available by ranking for number-based selection
        available_sorted = sorted(available, key=lambda n: rank_sort_key(n, rankings))
        display_available(available, rankings)

        while True:
            pick = input(f"{drafter} > ").strip()

            if pick.lower() == "quit":
                save_json(DRAFT_STATE_FILE, state)
                print("Draft saved. You can resume later.")
                return

            if pick.lower() == "help":
                print_help()
                continue

            if pick.lower() == "list":
                display_available(available, rankings)
                continue

            if pick.lower() == "picked":
                display_picked(state["picks"], state["teams"], rankings)
                continue

            if pick.lower() == "teams":
                display_teams(state["teams"], rankings)
                continue

            if not pick:
                continue

            # Try number-based selection (matches displayed rank-sorted order)
            if pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(available_sorted):
                    selected = available_sorted[idx]
                else:
                    print(f"Invalid number. Pick 1-{len(available_sorted)}.")
                    continue
            else:
                result = fuzzy_match(pick, available, rankings)
                if result is None:
                    print(f"No player found matching '{pick}'. Try again.")
                    continue
                elif isinstance(result, list):
                    print(f"Multiple matches:")
                    for i, m in enumerate(result, 1):
                        rank = rankings.get(m)
                        tag = f"(#{rank})" if rank else "(--)"
                        print(f"    {i}. {m} {tag}")
                    print("Be more specific or use the number from the main list.")
                    continue
                else:
                    selected = result

            # Show ranking context with confirmation
            rank = rankings.get(selected)
            rank_str = f" (OWGR #{rank})" if rank else " (unranked)"
            confirm = input(f"Confirm: {drafter} selects {selected}{rank_str}? (y/n): ").strip().lower()
            if confirm != "y":
                continue

            # Record the pick
            state["teams"][drafter].append(selected)
            state["available_players"].remove(selected)
            state["picks"].append({
                "pick_number": pick_num + 1,
                "round": current_round,
                "drafter": drafter,
                "player": selected,
                "world_ranking": rank,
                "timestamp": datetime.now().isoformat(),
            })
            state["current_pick"] = pick_num + 1
            save_json(DRAFT_STATE_FILE, state)

            print(f"\n>> {drafter} drafts {selected}{rank_str}! <<\n")
            break

    # Draft complete
    state["status"] = "complete"
    state["completed_at"] = datetime.now().isoformat()
    save_json(DRAFT_STATE_FILE, state)

    print("\n" + "=" * 50)
    print("   DRAFT COMPLETE!")
    print("=" * 50)
    display_teams(state["teams"], rankings)

    print("Draft order recap:")
    for pick in state["picks"]:
        rank = pick.get("world_ranking")
        rank_str = f"#{rank}" if rank else "--"
        print(f"  R{pick['round']} P{pick['pick_number']:>2}: {pick['drafter']:<10} -> {pick['player']:<28} {rank_str}")

    print(f"\nDraft state saved to: {DRAFT_STATE_FILE}")
    print("Good luck at the Masters!")


if __name__ == "__main__":
    run_draft()
