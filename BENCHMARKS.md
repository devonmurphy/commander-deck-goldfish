# Deck Benchmarks

Goldfish results for every profiled deck in `profiles/`, 300 games each,
15-turn cap, via `sim.run_simulation`. Regenerate anytime -- these numbers
drift slightly between runs (fresh RNG seed each call) and will change as
profiles/the engine evolve.

| Deck | Commander | Win Rate | Avg Win Turn | Median | Fastest |
|---|---|---|---|---|---|
| [Zeriam, Golden Wind](https://archidekt.com/decks/23913397/zeriam_golden_wind) | Zeriam, Golden Wind | 100.0% | 7.45 | 7.0 | 5 |
| [Azula Always Lies](https://archidekt.com/decks/23371168/azula_always_lies) | Fire Lord Azula | 87.3% | 8.37 | 8.0 | 4 |
| [Titania is Back](https://archidekt.com/decks/23684522/titania_is_back) | Titania, Protector of Argoth | 97.0% | 8.60 | 8.0 | 4 |
| [The Prismatic Bridge to Nowhere](https://archidekt.com/decks/1064159/the_prismatic_bridge_to_nowhere) | Esika, God of the Tree // The Prismatic Bridge | 92.7% | 9.86 | 10.0 | 6 |

## Win conditions, one line each

- **Zeriam**: double strike on the commander, then a random Griffin each
  turn after, snowballing combat-damage triggers -- win at 20 Griffin
  tokens (`win_tribal_token_type`/`win_tribal_token_threshold`).
- **Fire Lord Azula**: ramp into one huge X spell (`WIN_X_THRESHOLD`, the
  engine's generic default win condition).
- **Titania**: crack and replay fetch/sac lands out of the graveyard for
  extra land drops each turn, snowballing land-sacrifice-token triggers --
  win at 8 creatures (`win_creature_count_threshold`).
- **Esika // The Prismatic Bridge**: cast the commander's back face only,
  then copy its upkeep "reveal until creature/planeswalker, put it into
  play" trigger as many times as the board can afford each turn -- win at
  5 creatures with mana value 5+ (`win_creature_count_threshold` +
  `win_creature_min_cmc`).

See `.claude/skills/deck-profile/SKILL.md` for the full profile schema.
