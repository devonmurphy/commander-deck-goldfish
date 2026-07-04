#!/usr/bin/env python3
"""
Commander Deck Goldfish -- Web UI
----------------------------------
Paste a decklist (or an Archidekt deck URL), pick a game count, and hit
Goldfish. Runs that many solitaire games and reports, for every nonland
card, how often (and how early) it's actually castable.

Usage:
    python app.py
    (then open http://127.0.0.1:5000 in a browser)
"""

from flask import Flask, jsonify, render_template, request

import simulate as sim

app = Flask(__name__)

DEFAULT_GAMES = 1000
DEFAULT_MAX_TURNS = 10


def _int_field(form, name, default, minimum=1):
    raw = form.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"'{name}' must be a whole number, got '{raw}'.")
    if value < minimum:
        raise ValueError(f"'{name}' must be at least {minimum}.")
    return value


@app.route("/", methods=["GET", "POST"])
def index():
    form_values = {
        "decklist": "",
        "commander": "",
        "games": DEFAULT_GAMES,
        "max_turns": DEFAULT_MAX_TURNS,
    }
    result = None
    error = None

    if request.method == "POST":
        form_values["decklist"] = request.form.get("decklist", "").strip()
        form_values["commander"] = request.form.get("commander", "").strip()
        try:
            form_values["games"] = _int_field(request.form, "games", DEFAULT_GAMES)
            form_values["max_turns"] = _int_field(request.form, "max_turns", DEFAULT_MAX_TURNS)

            if not form_values["decklist"]:
                raise ValueError("Please paste a decklist or an Archidekt deck URL.")

            entries, commander_name = sim.resolve_decklist_input(
                form_values["decklist"], form_values["commander"]
            )
            result = sim.run_simulation(
                entries, commander_name, form_values["games"], form_values["max_turns"]
            )
        except (sim.DecklistError, ValueError) as e:
            error = str(e)
        except Exception as e:
            error = f"Something went wrong fetching card data: {e}"

    return render_template("index.html", form_values=form_values, result=result, error=error)


@app.route("/replay", methods=["POST"])
def replay():
    """Re-simulates one specific game (by index) for the game player, using
    the same decklist/commander/turn settings as the original run. Returns
    JSON: {"frames": [...], "win_turn": ..., "game_index": ...}."""
    try:
        decklist = request.form.get("decklist", "").strip()
        commander_override = request.form.get("commander", "").strip()
        game_index = _int_field(request.form, "game_index", 0, minimum=0)
        max_turns = _int_field(request.form, "max_turns", DEFAULT_MAX_TURNS)
        run_seed = _int_field(request.form, "run_seed", 0, minimum=0)

        if not decklist:
            raise ValueError("Missing decklist.")

        entries, commander_name = sim.resolve_decklist_input(decklist, commander_override)
        replay_data = sim.replay_single_game(entries, commander_name, game_index, max_turns, run_seed)
        return jsonify(replay_data)
    except (sim.DecklistError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True)
