---
name: deck-profile
description: Analyze a Commander deck's commander and build a play-sequencing "profile" JSON for the Commander Deck Goldfish simulator. Use when the user wants to create or update a profile for a commander in this project, or asks things like "make a profile for this deck" / "set up goldfish for my new commander".
---

# Deck Profile Builder

The Commander Deck Goldfish simulator (`simulate.py` in this project) plays a
deck out solo to see how often each card is castable by a given turn. Most
of its engine is generic (mana costs, mana rocks, rituals, Treasures, X
spells, mulligans) and needs no configuration. But some commanders have
sequencing-relevant abilities that aren't inferable from oracle text alone
-- how *you'd actually choose to pilot the deck*. That's what a profile
captures, as a JSON file in `profiles/`, auto-applied by commander name.

## Schema

The authoritative schema is `DEFAULT_PROFILE` in `simulate.py` -- read it
before writing a profile, in case fields have been added or renamed since
this skill was written. As of writing, the fields are:

- `commander` (string): exact Scryfall card name. Used to match the profile
  to a deck automatically.
- `hold_until_commander_resolves` (list of strings): card types or keywords
  (matched case-insensitively against type_line or oracle text) to keep in
  hand and not cast until the commander has been cast. Example: `["Instant",
  "Flash"]` for a deck that wants to hold up spells until the commander is
  down to benefit from an attack trigger. Empty list = hold nothing.
- `commander_copies_spells_while_attacking` (bool): if the commander has a
  "whenever you cast a spell while attacking, copy it" style ability, set
  this true. The engine already assumes any commander with an attack
  trigger starts attacking the turn after it resolves; this flag doubles
  the effect (mana, treasures, permanents entering play) of anything cast
  during those turns.
- `reserve_mana_kinds_for_x_spells` (list of strings): mana "kinds" to bank
  and spend ONLY on X-cost spells rather than on whatever's castable. Today
  the only supported kind is `"treasure"`. Use this for decks whose plan is
  clearly "stockpile mana rocks/treasures, then dump it all into one huge X
  spell" rather than spending ramp piecemeal.
- `win_tribal_token_type` / `win_tribal_token_threshold`: for decks that win
  by making N tokens of a creature type via combat damage (e.g. Zeriam,
  Golden Wind: "make 10 Griffins") instead of casting a big X spell. Setting
  these turns on a whole extra combat-simulation subsystem in `simulate.py`
  (`_run_combat_step`) that's otherwise a complete no-op for every other
  profile. The engine auto-detects the actual trigger ("whenever a Griffin
  you control deals combat damage to a player, create ... token") from the
  commander's oracle text via `_parse_tribal_combat_damage_trigger` -- these
  two fields just say which tribe to count and when that count means "win."
  Every creature without summoning sickness is assumed to attack unblocked
  (no opponent/combat model exists otherwise). Token doublers ("create twice
  that many tokens instead" -- Anointed Procession, Mondrak, etc.) are also
  auto-detected from oracle text and stack multiplicatively.
- `double_strike_auto_commander_cards` / `double_strike_single_target_cards`
  / `double_strike_blanket_cards` / `double_strike_propagator_cards`: only
  meaningful alongside `win_tribal_token_type`. Card names (by how they
  grant double strike) that decide who has double strike each combat --
  auto-commander (free, e.g. Flaming Fist), single-target (locks double
  strike onto the commander first, then a random creature of the win tribe,
  one per turn, e.g. Twinblade Blessing/Genji Glove), blanket (every
  attacker gets it while in play, e.g. True Conviction), propagator (spreads
  double strike to the whole team once any one creature has it, e.g. Odric,
  Lunarch Marshal). See `profiles/zeriam_golden_wind.json` for a worked
  example.
- `roaming_throne_chosen_type`: if the deck runs Roaming Throne, the
  creature type assumed chosen when it enters. Only has an effect when it
  matches `win_tribal_token_type` -- doubles the tribal combat-damage
  trigger itself (on top of any token doublers).

Note: `simulate.py` does NOT track +1/+1 counters (e.g. from Cathars'
Crusade) -- tokens are evaluated at their base printed power/toughness for
any "draw when a small creature enters" triggers (`ETB_DRAW_TRIGGERS`).
This is a deliberate simplification, not a gap to fix per-profile.

Note what's already generic and does NOT need a profile entry: "Firebending
N" is auto-detected from oracle text (see `_parse_firebending`); regular
mana rocks, rituals, Treasure-token math, and "draw N (discard M)" card-draw
effects are all auto-detected from oracle text too (see `resolve_mana_profile`
and `resolve_draw_profile`). Only add a profile field for something the
engine can't already infer on its own.

## Process

1. **Get the decklist.** Accept either a pasted decklist or an Archidekt
   deck URL. If given a URL, use `simulate.resolve_decklist_input()` (or
   just fetch `https://archidekt.com/api/decks/<id>/` directly) to get the
   card list and the commander's name from the "Commander" category.
   If the user pasted a plain list with no commander marked, ask which
   card is the commander.

2. **Read the commander's oracle text.** Fetch it from Scryfall
   (`GET https://api.scryfall.com/cards/named?exact=<name>`). Look for:
   - An attack trigger that adds mana or has other effects ("Whenever this
     creature attacks...", named keywords like Firebending).
   - A "whenever you cast a spell/instant/sorcery..." trigger, especially
     one that's clearly better used AFTER the commander is out (copy
     effects, storm-style payoffs, cost reduction that only matters once
     board state exists).
   - Explicit spell-copying language ("copy that spell" / "copy it").
   - Anything suggesting the deck's plan revolves around banking mana
     (Treasures, mana rocks) for one big X spell rather than curving out.

3. **Skim a handful of nonland, non-obvious cards** in the list (especially
   ones the commander's ability interacts with) to sanity-check your read
   of the deck's plan. You don't need to check all 99 -- just enough to
   confirm the commander's ability is the deck's actual engine and not a
   minor upside.

4. **Ask the user clarifying questions** for anything the oracle text
   doesn't settle on its own. Use the AskUserQuestion tool. Good questions
   look like:
   - "Should Instant/Flash spells be held until the commander resolves, or
     cast on curve as normal?" (only ask if the commander has some benefit
     tied to attacking/being out that would make holding worthwhile)
   - "Does this commander copy spells cast while attacking?" (confirm your
     oracle-text read rather than assuming)
   - "Does this deck want to save Treasures/mana rocks for one big X spell,
     or spend mana as soon as it's useful?"
   - Anything else specific to that commander's build-around that doesn't
     map cleanly to the existing schema -- if you find a real sequencing
     rule the schema can't express, say so explicitly rather than forcing
     it into an existing field, and propose a new field (added to
     `DEFAULT_PROFILE` and threaded through `simulate_game` in `simulate.py`)
     instead of silently ignoring it.

5. **Write the profile** to `profiles/<slug>.json`, where `<slug>` is the
   commander's name lowercased with non-alphanumeric runs collapsed to
   underscores (e.g. "Fire Lord Azula" -> `fire_lord_azula.json`). Use
   `commander-deck-goldfish/profiles/fire_lord_azula.json` as a reference
   example of the format.

6. **Confirm with the user**: show the final JSON, where it was saved, and
   remind them it auto-applies next time they goldfish that commander in
   the web UI (`run_simulation` calls `find_profile_for_commander()`
   automatically when no profile is passed explicitly).

## Notes

- Don't invent schema fields the engine doesn't actually read -- check
  `simulate_game()` uses whatever you add, or add the wiring yourself.
- If the commander doesn't have any sequencing-relevant ability at all,
  say so and skip creating a profile -- the generic engine defaults are
  already correct for a normal curve-out deck.
