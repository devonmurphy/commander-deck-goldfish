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

from flask import Flask, render_template, request

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


if __name__ == "__main__":
    app.run(debug=True)
