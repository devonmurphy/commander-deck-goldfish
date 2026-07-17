"""
Commander Deck Goldfish Simulator
----------------------------------
Paste a decklist (or an Archidekt deck URL), and this plays it out against
no opponent ("goldfishing") a bunch of times to see how often each spell is
actually castable by a given turn.

Simplifications (this is a goldfish tool, not a rules engine):
  - No opponent, no combat, no removal, no interaction of any kind.
  - Lands/rocks are assumed to always enter untapped (ignores "enters tapped
    unless..." conditions on checklands/slowlands/etc.).
  - Mana-rock activation costs (e.g. Signets' {1}) aren't subtracted --
    they're treated the same as a land that taps for their "Add" text.
  - X spells are held until last each turn, then cast for the maximum X
    the remaining mana allows (rather than being fully modeled/optimized).
  - Phyrexian mana pips are treated as always payable (assume you pay life).
  - Numeric/color hybrid pips (e.g. {2/W}) are folded into the generic cost.
  - Split cards (Fire // Ice) are costed using only their first printed face.
  - Mana payment uses a greedy matcher, not a true optimal solver -- it can
    occasionally misjudge castability in gnarly multi-hybrid edge cases.
"""

import json
import os
import random
import re
import statistics

import requests

PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")

# A deck "profile" captures sequencing/strategy decisions that aren't
# inferable from oracle text alone -- how *you'd* actually pilot this deck.
# Decks without a matching profile just use these generic defaults.
#   hold_until_commander_resolves: card types/keywords (matched against
#     type_line or oracle text, case-insensitive) to keep in hand and not
#     cast until the commander has resolved, e.g. ["Instant", "Flash"].
#   commander_copies_spells_while_attacking: if true, any spell cast while
#     the commander is attacking (turn > the turn it was cast) has its mana
#     output doubled, modeling a copy of that spell also resolving.
#   reserve_mana_kinds_for_x_spells: mana "kinds" (currently just
#     "treasure") that are banked and spent ONLY on X spells rather than on
#     whatever's castable -- for decks planning to dump everything into one
#     big X spell instead of using ramp piecemeal.
#   win_tribal_token_type / win_tribal_token_threshold: for decks that win by
#     making N tokens of a given creature type via combat damage (e.g. "make
#     10 Griffins") rather than casting a big X spell. Setting these turns on
#     a whole extra combat-simulation subsystem (see _run_combat_step) that's
#     otherwise a complete no-op -- every other profile/deck is unaffected.
#   double_strike_auto_commander_cards: cards that give the commander double
#     strike automatically on attack, no action/mana needed (e.g. Flaming
#     Fist's Background ability).
#   double_strike_single_target_cards: cards that, once in play, let the
#     pilot lock double strike onto one creature per turn -- the commander
#     first if she doesn't have it yet, else a random creature of the win
#     tribe.
#   double_strike_blanket_cards: cards that give every attacking creature
#     double strike for the turn while in play (or, for one-shot instants,
#     the turn they're cast).
#   double_strike_propagator_cards: cards (e.g. Odric) that give every
#     attacker double strike once ANY creature you control already has it.
#   roaming_throne_chosen_type: if the deck runs Roaming Throne, the creature
#     type assumed chosen -- when it matches win_tribal_token_type, doubles
#     the tribal combat-damage trigger.
#   win_creature_count_threshold: for decks that win by amassing N creatures
#     via a land-sacrifice/recursion engine (e.g. Titania, Protector of
#     Argoth: "make 8 dudes") rather than combat or a big X spell. Setting
#     this turns on a whole extra land-lifecycle subsystem (multiple land
#     drops per turn, fetch/sac-lands actually leaving play into a land
#     graveyard, "play lands from your graveyard" recursion, land-sac token
#     triggers) that's otherwise a complete no-op -- every other
#     profile/deck plays exactly one land per turn from hand as before.
#   win_creature_min_cmc: optional filter on win_creature_count_threshold --
#     when set, only creatures with mana value >= this count toward the
#     threshold (e.g. Esika // The Prismatic Bridge's "5+ big boiz": the
#     deck's own cheap copy-trigger utility creatures shouldn't count, only
#     the big uncastable bombs the Bridge actually puts into play). None
#     (the default) means every creature counts, same as before this field
#     existed.
DEFAULT_PROFILE = {
    "commander": None,
    "hold_until_commander_resolves": [],
    "commander_copies_spells_while_attacking": False,
    "reserve_mana_kinds_for_x_spells": [],
    "win_tribal_token_type": None,
    "win_tribal_token_threshold": None,
    "double_strike_auto_commander_cards": [],
    "double_strike_single_target_cards": [],
    "double_strike_blanket_cards": [],
    "double_strike_propagator_cards": [],
    "roaming_throne_chosen_type": None,
    "win_creature_count_threshold": None,
    "win_creature_min_cmc": None,
}


def load_profile(name_or_path):
    """Loads a profile JSON file, either by path or by name (looked up in
    the profiles/ directory, with or without a .json extension)."""
    path = name_or_path
    if not os.path.isfile(path):
        path = os.path.join(PROFILES_DIR, name_or_path)
        if not path.endswith(".json"):
            path += ".json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {**DEFAULT_PROFILE, **data}


def list_profiles():
    """Returns every profile in profiles/ as {"file": name, **profile}."""
    if not os.path.isdir(PROFILES_DIR):
        return []
    profiles = []
    for filename in sorted(os.listdir(PROFILES_DIR)):
        if filename.endswith(".json"):
            profile = load_profile(filename)
            profile["file"] = filename
            profiles.append(profile)
    return profiles


def find_profile_for_commander(commander_name):
    """Returns the profile whose "commander" field case-insensitively
    matches, or DEFAULT_PROFILE if none is found."""
    if commander_name:
        for profile in list_profiles():
            if (profile.get("commander") or "").lower() == commander_name.lower():
                return profile
    return dict(DEFAULT_PROFILE)


SCRYFALL_ROOT = "https://api.scryfall.com"
HEADERS = {
    "User-Agent": "CommanderDeckGoldfish/1.0 (personal use script)",
    "Accept": "application/json",
}

BASIC_LAND_BY_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}
ALL_COLORS = ["W", "U", "B", "R", "G"]
TREASURE_TOKEN_IMAGE_URL = "https://cards.scryfall.io/normal/front/c/6/c6e096bb-ad9e-4a8b-8b42-26852fa32c1d.jpg"

ARCHIDEKT_URL_RE = re.compile(r"archidekt\.com/decks/(\d+)", re.I)
_MANA_TOKEN_RE = re.compile(r"\{([^}]+)\}")
_ADD_CLAUSE_RE = re.compile(r"[Aa]dd ([^.]+)\.")
_TREASURE_RE = re.compile(r"create (a|an|one|two|three|four|five|six|\d+) treasure tokens?")
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _word_to_number(word):
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word, 1)


def _to_int_or_none(value):
    """Scryfall power/toughness are strings, and can be non-numeric ("*",
    "1+*") for variable-P/T creatures -- treat those as unknown rather than
    guessing, so ETB-draw-trigger power checks just don't fire for them."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Decklist input
# ---------------------------------------------------------------------------

class DecklistError(Exception):
    """Raised when the pasted input can't be turned into a decklist."""


def parse_decklist_text(text):
    """Parse lines like '1x Sol Ring', '1 Sol Ring', or 'Sol Ring' into a
    list of (quantity, name). Blank lines and '//' comments are skipped."""
    entries = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        match = re.match(r"^(\d+)\s*x?\s+(.+)$", line, re.I)
        if match:
            qty, name = int(match.group(1)), match.group(2).strip()
        else:
            qty, name = 1, line
        if name:
            entries.append((qty, name))
    if not entries:
        raise DecklistError("Couldn't find any card names in that decklist.")
    return entries


def fetch_archidekt_decklist(url):
    """Pull a decklist straight from an Archidekt deck URL, returning
    ([(qty, name), ...], commander_name_or_None). Cards in the Maybeboard or
    Sideboard categories are excluded."""
    match = ARCHIDEKT_URL_RE.search(url)
    if not match:
        raise DecklistError(f"'{url}' doesn't look like an Archidekt deck URL.")
    deck_id = match.group(1)

    resp = requests.get(
        f"https://archidekt.com/api/decks/{deck_id}/",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    if resp.status_code == 404:
        raise DecklistError(f"Archidekt has no deck at that URL (id {deck_id}).")
    resp.raise_for_status()
    data = resp.json()

    entries = []
    commander_name = None
    for card_entry in data.get("cards", []):
        categories = card_entry.get("categories") or []
        if "Maybeboard" in categories or "Sideboard" in categories:
            continue
        name = card_entry["card"]["oracleCard"]["name"]
        qty = card_entry.get("quantity", 1)
        if "Commander" in categories:
            commander_name = name
        else:
            entries.append((qty, name))

    if not entries and not commander_name:
        raise DecklistError("That Archidekt deck appears to be empty.")
    return entries, commander_name


def resolve_decklist_input(raw_input, commander_override=None):
    """Accepts either a pasted decklist or an Archidekt URL. Returns
    ([(qty, name), ...], commander_name_or_None)."""
    stripped = raw_input.strip()
    if stripped.lower().startswith("http") and ARCHIDEKT_URL_RE.search(stripped):
        entries, commander_name = fetch_archidekt_decklist(stripped)
    else:
        entries = parse_decklist_text(stripped)
        commander_name = None

    if commander_override:
        commander_override = commander_override.strip()
        if commander_override:
            commander_name = commander_override
            entries = [(q, n) for q, n in entries if n.lower() != commander_override.lower()]

    return entries, commander_name


# ---------------------------------------------------------------------------
# Scryfall card data
# ---------------------------------------------------------------------------

def fetch_card_data(names):
    """Batch-fetch card info from Scryfall's /cards/collection endpoint (up
    to 75 identifiers per request). Returns {name: card_info_dict}."""
    info = {}
    unique_names = list(dict.fromkeys(names))
    for i in range(0, len(unique_names), 75):
        batch = unique_names[i:i + 75]
        # The collection endpoint matches split/MDFC cards by their front
        # face's name, not the combined "Front // Back" string -- querying
        # with the full name silently returns not_found for those layouts.
        query_names = [n.split(" // ")[0] for n in batch]
        resp = requests.post(
            f"{SCRYFALL_ROOT}/cards/collection",
            headers=HEADERS,
            json={"identifiers": [{"name": n} for n in query_names]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for card in data.get("data", []):
            parsed = _parse_scryfall_card(card)
            possible_keys = [card["name"]] + [
                face.get("name") for face in card.get("card_faces") or []
            ]
            matched_key = next((k for k in possible_keys if k in unique_names), card["name"])
            info[matched_key] = parsed
    return info


def _parse_scryfall_card(card):
    type_line = card.get("type_line", "")
    mana_cost = card.get("mana_cost", "")
    cmc = card.get("cmc", 0)
    oracle_text = card.get("oracle_text") or ""
    power = card.get("power")
    toughness = card.get("toughness")
    if not mana_cost and card.get("card_faces"):
        front = card["card_faces"][0]
        mana_cost = front.get("mana_cost", "") or mana_cost
        cmc = front.get("cmc", cmc)
        oracle_text = oracle_text or front.get("oracle_text") or ""
        power = power if power is not None else front.get("power")
        toughness = toughness if toughness is not None else front.get("toughness")

    image_uris = card.get("image_uris")
    if image_uris:
        image_url = image_uris.get("normal") or image_uris.get("large") or image_uris.get("small")
    else:
        image_url = None
        for face in card.get("card_faces") or []:
            face_images = face.get("image_uris")
            if face_images:
                image_url = face_images.get("normal") or face_images.get("large") or face_images.get("small")
                break

    parsed = {
        "name": card["name"],
        "type_line": type_line,
        "is_land": "Land" in type_line,
        "mana_cost": mana_cost,
        "cmc": cmc,
        "oracle_text": oracle_text,
        "color_identity": card.get("color_identity", []),
        "produced_mana": card.get("produced_mana"),
        "image_url": image_url,
        "power": _to_int_or_none(power),
        "toughness": _to_int_or_none(toughness),
    }
    return _apply_card_overrides(parsed)


# A handful of real cards don't fit the generic oracle-text parsers -- exotic
# layouts (Blazing Firesinger's "prepare" MDFC), or modal spells where we
# just assume a specific line of play. Rather than special-case every one of
# these deep in the simulation logic, rewrite their parsed data into a form
# the EXISTING generic detectors (resolve_mana_profile, resolve_draw_profile)
# already understand correctly.
_CARD_OVERRIDES = {
    "Blazing Firesinger // Seething Song": {
        "type_line": "Instant",
        "mana_cost": "{2}{R}",
        "cmc": 3,
        "oracle_text": "Add {R}{R}{R}{R}{R}.",
        "produced_mana": ["R"],
    },
    "Ashling's Command": {
        "oracle_text": (
            "Create two Treasure tokens. "
            '(They\'re artifacts with "{T}, Sacrifice this token: Add one mana of any color.") '
            "Draw two cards."
        ),
        "produced_mana": ["W", "U", "B", "R", "G"],
    },
    "Three Steps Ahead": {
        # Spree modes: dynamic cost handled by name in simulate_game
        # (_three_steps_ahead_cost_and_target); this override just gets the
        # generic draw-effect detector to pick up the "draw two, discard
        # one" mode we always assume.
        "oracle_text": "Draw two cards, then discard a card.",
    },
    "Mystic Confluence": {
        # "Choose three, may repeat" among counter/bounce/draw -- assume
        # two draws and one bounce (the bounce itself isn't modeled).
        "oracle_text": "Draw two cards.",
    },
    "Fall of the Titans": {
        # Normal cost is {X}{X}{R} (X counted twice) -- our X-spell handling
        # assumes a single X, so it'd effectively double the real X. Assume
        # we always have Surge available (very likely in a spell-dense
        # deck) and use that single-X cost instead.
        "mana_cost": "{X}{R}",
    },
    "Comet Storm": {
        # Multikicker {1}, assume a 4-player pod: the first target is free,
        # then +{1} per additional opponent to hit all 3.
        "mana_cost": "{X}{2}{R}{R}",
    },
    "Esika, God of the Tree // The Prismatic Bridge": {
        # This deck's whole plan is the back face's upkeep trigger -- always
        # cast (and played as commander) as The Prismatic Bridge, never as
        # the front face's mana-dork mode. See profile
        # esika_god_of_the_tree_the_prismatic_bridge.json.
        "type_line": "Legendary Enchantment",
        "mana_cost": "{W}{U}{B}{R}{G}",
        "cmc": 5,
        "oracle_text": (
            "At the beginning of your upkeep, reveal cards from the top of your library "
            "until you reveal a creature or planeswalker card. Put that card onto the "
            "battlefield and the rest on the bottom of your library in a random order."
        ),
        "power": None,
        "toughness": None,
    },
}


def _apply_card_overrides(card_info):
    overrides = _CARD_OVERRIDES.get(card_info["name"])
    if overrides:
        card_info.update(overrides)
        card_info["is_land"] = "Land" in card_info["type_line"]
    return card_info


# ---------------------------------------------------------------------------
# Mana modeling
# ---------------------------------------------------------------------------

_ACTIVATION_COST_PREFIX_RE = re.compile(r"((?:\{[^}]+\}, )*)\{T\}: Add")


def _parse_activation_cost(text):
    """Sums the generic mana cost that precedes a mana ability's {T} symbol
    -- e.g. a Signet's leading {1} in "{1}, {T}: Add {U}{B}." (which nets
    only +1 mana per use, not a free +2). Returns 0 for abilities that are
    just "{T}: Add ..." with no extra cost (Sol Ring, Talismans, etc.)."""
    match = _ACTIVATION_COST_PREFIX_RE.search(text)
    if not match:
        return 0
    return sum(int(tok) for tok in _MANA_TOKEN_RE.findall(match.group(1)) if tok.isdigit())


def resolve_mana_profile(card_info, deck_colors, land_type_lines=None):
    """Figures out what a card (land, mana rock, ritual spell, mana dork --
    anything) contributes to the mana pool. Returns None if it isn't a mana
    source at all. Otherwise:
        {"kind": "permanent" | "ritual", "is_fetch": bool,
         "fetch_colors": set(colors), "produces": set(colors), "amount": int,
         "activation_cost": int}
    "permanent" sources (lands, rocks, dorks) keep producing every future
    turn; "ritual" sources (Dark Ritual, Seething Song, ...) only add mana
    for the turn they're cast. `activation_cost` is generic mana that must
    be paid EACH TIME a "permanent" source is tapped (e.g. a Signet's {1}) --
    0 for lands and cost-free rocks, which just produce for free. `land_type_lines`
    (type_lines of every OTHER land in the deck) lets a fetchland that only
    names one or two basic types (e.g. Bloodstained Mire: "Mountain or Swamp")
    reach any other color it can actually fetch via a nonbasic dual/shock
    land sharing one of those types (e.g. Watery Grave has the Swamp type,
    so it's fetchable too, and brings blue along with it)."""
    text = card_info["oracle_text"]
    if "Firebending" in text:
        return None  # attack-triggered, handled separately in simulate_game
    text_lower = text.lower()
    type_line = card_info["type_line"]
    is_land = card_info["is_land"]
    is_instant_or_sorcery = "Instant" in type_line or "Sorcery" in type_line

    # Classic fetchlands (Bloodstained Mire, etc.) name specific basic types
    # ("a Swamp or Mountain card") instead of saying "land card" -- catch
    # both phrasings.
    is_land_search = "search your library for" in text_lower and (
        "land card" in text_lower
        or any(basic.lower() in text_lower for basic in BASIC_LAND_BY_COLOR.values())
    )
    if is_land_search:
        named_colors = {color for color, basic in BASIC_LAND_BY_COLOR.items() if basic in text}
        target_basics = {basic for color, basic in BASIC_LAND_BY_COLOR.items() if basic in text}
        fetch_colors = set(named_colors)
        for other_type_line in land_type_lines or []:
            other_basics = {basic for basic in BASIC_LAND_BY_COLOR.values() if basic in other_type_line}
            if other_basics & target_basics:
                fetch_colors |= {c for c, basic in BASIC_LAND_BY_COLOR.items() if basic in other_basics}
        fetch_colors = fetch_colors or (set(deck_colors) or {"C"})
        return {"kind": "permanent", "is_fetch": True, "fetch_colors": fetch_colors,
                "produces": set(), "amount": 1, "activation_cost": 0}

    if "any color" in text_lower:
        treasure_match = _TREASURE_RE.search(text_lower)
        if treasure_match:
            # Unlike a ritual's burst, Treasures are actual permanents that
            # stick around until sacrificed -- banked separately so they
            # carry over to later turns instead of evaporating unused.
            return {"kind": "treasure", "is_fetch": False, "fetch_colors": set(),
                    "produces": set(deck_colors) or {"C"}, "amount": _word_to_number(treasure_match.group(1)),
                    "activation_cost": 0}
        return {"kind": "ritual" if is_instant_or_sorcery else "permanent",
                "is_fetch": False, "fetch_colors": set(),
                "produces": set(deck_colors) or {"C"}, "amount": 1, "activation_cost": 0}

    if not card_info.get("produced_mana"):
        return None

    add_clauses = _ADD_CLAUSE_RE.findall(text)
    if not add_clauses:
        return None

    activation_cost = 0
    if len(add_clauses) > 1 or any(" or " in clause for clause in add_clauses):
        # Modal ("Add {C}." / "Add {U} or {B}.") -- one mana per activation,
        # but it could be any of the colors mentioned across the options.
        tokens = set()
        for clause in add_clauses:
            tokens |= set(_MANA_TOKEN_RE.findall(clause))
        produces = tokens or set(card_info["produced_mana"])
        amount = 1
    else:
        # A single non-modal clause: every symbol is added at once
        # (Sol Ring's {C}{C}, Dark Ritual's {B}{B}{B}, a Signet's {U}{B}).
        tokens = _MANA_TOKEN_RE.findall(add_clauses[0])
        produces = set(tokens) if tokens else set(card_info["produced_mana"])
        amount = len(tokens) if tokens else 1
        activation_cost = _parse_activation_cost(text)

    kind = "permanent" if is_land or not is_instant_or_sorcery else "ritual"
    if kind != "permanent":
        activation_cost = 0  # one-shot rituals just pay their cast cost, no separate activation
    return {"kind": kind, "is_fetch": False, "fetch_colors": set(), "produces": produces,
            "amount": amount, "activation_cost": activation_cost}


def parse_mana_cost(mana_cost):
    """Returns (generic_amount, colored_pips) where colored_pips is a list
    of sets, each set being the colors that could satisfy that one pip."""
    generic = 0
    colored_pips = []
    for token in _MANA_TOKEN_RE.findall(mana_cost or ""):
        if token.isdigit():
            generic += int(token)
        elif token == "X":
            continue  # X spells costed as X=0
        elif token in ("W", "U", "B", "R", "G", "C"):
            colored_pips.append({token})
        elif "/" in token:
            parts = token.split("/")
            if "P" in parts:
                continue  # Phyrexian mana -- assume paid with life
            if all(p in ("W", "U", "B", "R", "G", "C") for p in parts):
                colored_pips.append(set(parts))  # hybrid, e.g. {W/U}
            else:
                numeric = next((p for p in parts if p.isdigit()), None)
                if numeric is not None:
                    generic += int(numeric)  # e.g. {2/W} -> assume generic
                else:
                    colored_pips.append(set(parts))
        else:
            generic += 1  # snow ({S}) and anything unrecognized
    return generic, colored_pips


def try_pay(available_sources, generic, colored_pips):
    """available_sources: list of {"name": str, "colors": frozenset(colors)}
    mana sources. Greedy matcher (not a true optimal solver): satisfies the
    most color-constrained pips first, preferring the least-flexible
    matching source so duals are saved for later pips. Returns the list of
    source indices that would be spent, or None if it can't be paid."""
    indexed = list(enumerate(available_sources))
    used = set()
    for pip in sorted(colored_pips, key=len):
        candidates = [i for i, src in indexed if i not in used and src["colors"] & pip]
        if not candidates:
            return None
        best = min(candidates, key=lambda i: len(indexed[i][1]["colors"]))
        used.add(best)
    if len(indexed) - len(used) < generic:
        return None
    remaining = [i for i, _ in indexed if i not in used]
    used.update(remaining[:generic])
    return list(used)


# ---------------------------------------------------------------------------
# Card draw modeling
# ---------------------------------------------------------------------------

_DRAW_RE = re.compile(r"draw (a|an|one|two|three|four|five|six|seven|\d+) cards?")
_DISCARD_RE = re.compile(r"discard (a|an|one|two|three|four|\d+) cards?")


def resolve_draw_profile(card_info):
    """Detects simple 'draw N cards' / 'discard M, then draw N' effects
    (Thrill of Possibility, etc.) from oracle text. Returns
    {"draw": N, "discard": M} or None if the card doesn't draw cards.
    Doesn't handle conditional or scaling draw ("draw a card for each...")."""
    text_lower = card_info["oracle_text"].lower()
    draw_match = _DRAW_RE.search(text_lower)
    if not draw_match:
        return None
    discard_match = _DISCARD_RE.search(text_lower)
    return {
        "draw": _word_to_number(draw_match.group(1)),
        "discard": _word_to_number(discard_match.group(1)) if discard_match else 0,
    }


# ---------------------------------------------------------------------------
# Land-untap modeling (Snap, etc.) -- these effectively refund mana you
# already spent this turn. "Untap ALL lands" (Turnabout) and "untap X
# permanents" tied to an X cost (Reality Spasm) don't fit a fixed-count
# regex and are special-cased directly in simulate_game by name.
# ---------------------------------------------------------------------------

_UNTAP_LANDS_RE = re.compile(r"untap (?:up to )?(a|an|one|two|three|four|five|\d+) lands?\b")


def resolve_untap_lands(card_info):
    """Detects fixed-count 'untap N lands' effects. Returns N or None."""
    match = _UNTAP_LANDS_RE.search(card_info["oracle_text"].lower())
    return _word_to_number(match.group(1)) if match else None


def _tapped_source_names(all_sources, available):
    """Names of sources present in `all_sources` but no longer sitting in
    `available` -- i.e. tapped for mana at some point this turn. They stay
    tapped until your next untap step, not just until end of turn."""
    remaining = list(available)
    tapped = []
    for source in all_sources:
        if source in remaining:
            remaining.remove(source)
        else:
            tapped.append(source["name"])
    return tapped


def _refund_lands(count, battlefield_sources, battlefield_lands, available):
    """Moves up to `count` currently-tapped land sources back into
    `available` (mutated in place). "Tapped" = a land source present in
    battlefield_sources but not currently in available."""
    if count <= 0:
        return 0
    all_lands = [s for s in battlefield_sources if s["name"] in battlefield_lands]
    tapped = list(all_lands)
    for s in available:
        if s["name"] in battlefield_lands and s in tapped:
            tapped.remove(s)
    refund = tapped[:count]
    available.extend(refund)
    return len(refund)


def _activate_costed_rocks(rocks, available):
    """Taps each rock that has a per-use activation cost (e.g. a Signet's
    "{1}, {T}: Add {U}{B}.") if `available` can cover it -- these aren't
    free like a land, so they're not sitting in battlefield_sources; we pay
    the cost and produce the mana fresh each time. Returns
    (updated_available, activated_names) -- activated_names lists which
    rocks actually got tapped this call, since they're invisible to
    _tapped_source_names otherwise (does not mutate the input)."""
    activated_names = []
    for rock in rocks:
        cost = rock["activation_cost"]
        if cost <= len(available):
            available = available[cost:] + [
                {"name": rock["source_name"], "colors": frozenset(rock["produces"])}
                for _ in range(rock["amount"])
            ]
            activated_names.append(rock["source_name"])
    return available, activated_names


def _choose_discard(hand, battlefield_sources):
    """Discards the least useful card: a land that would add the fewest
    new colors if there's a spare one, otherwise the priciest spell."""
    lands_in_hand = [c for c in hand if c["is_land"]]
    if lands_in_hand:
        have = {color for source in battlefield_sources for color in source["colors"]}

        def new_color_count(land):
            profile = land["_mana_profile"]
            if not profile:
                return -1
            pool = profile["fetch_colors"] if profile["is_fetch"] else profile["produces"]
            return len(pool - have)

        return min(lands_in_hand, key=new_color_count)

    nonlands = [c for c in hand if not c["is_land"]]
    return max(nonlands, key=lambda c: c["cmc"]) if nonlands else None


def _discard_cards(hand, count, battlefield_sources):
    """Discards up to `count` cards from hand and returns the ones actually
    discarded (by reference), so callers can also drop them from any other
    per-turn candidate list -- duplicate card names would collide under
    plain equality, so identity is what matters here."""
    discarded = []
    for _ in range(count):
        discard = _choose_discard(hand, battlefield_sources)
        if discard is None:
            break
        hand.remove(discard)
        discarded.append(discard)
    return discarded


# ---------------------------------------------------------------------------
# Game simulation
# ---------------------------------------------------------------------------

MULLIGAN_MIN_LANDS = 3
MULLIGAN_MAX_LANDS = 5
MAX_MULLIGAN_ATTEMPTS = 200


def _draw_opening_hand(library, rng):
    hand = library[:7]
    for _ in range(MAX_MULLIGAN_ATTEMPTS):
        rng.shuffle(library)
        hand = library[:7]
        lands = sum(1 for c in hand if c["is_land"])
        if MULLIGAN_MIN_LANDS <= lands <= MULLIGAN_MAX_LANDS:
            return hand, library[7:]
    return hand, library[7:]  # give up, keep the last hand drawn


def _choose_fetch_color(fetch_colors, current_sources, hand):
    if not fetch_colors:
        return None
    have = {c for src in current_sources for c in src["colors"]}
    missing_useful = set()
    for card in hand:
        if card["is_land"]:
            continue
        _, pips = parse_mana_cost(card["mana_cost"])
        for pip in pips:
            if not (pip & have):
                missing_useful |= (pip & fetch_colors)
    if missing_useful:
        return next(iter(missing_useful))
    new_colors = fetch_colors - have
    if new_colors:
        return next(iter(new_colors))
    return next(iter(fetch_colors))


def _choose_land_to_play(hand_lands, current_sources):
    have = {c for src in current_sources for c in src["colors"]}

    def new_color_count(land):
        profile = land["_mana_profile"]
        if not profile:
            return -1  # a land with no mana ability at all -- last resort
        pool = profile["fetch_colors"] if profile["is_fetch"] else profile["produces"]
        return len(pool - have)

    return max(hand_lands, key=new_color_count)


def _apply_mana_source(profile, battlefield_sources, available, hand, source_name):
    """Adds the mana a just-played/cast source provides to `available`
    (this turn) and, if it's a permanent source, to `battlefield_sources`
    (every future turn too). Mutates both lists in place. Each source is
    {"name": source_name, "colors": frozenset(colors)} so later cast steps
    can report exactly which permanent got tapped. Returns the fetched
    color for fetch lands (for logging), else None."""
    if profile["is_fetch"]:
        color = _choose_fetch_color(profile["fetch_colors"], battlefield_sources, hand)
        if color is None:
            return None
        source = {"name": source_name, "colors": frozenset({color})}
        battlefield_sources.append(source)
        available.append(source)
        return color

    if not profile["produces"]:
        return None
    new_sources = [
        {"name": source_name, "colors": frozenset(profile["produces"])}
        for _ in range(profile["amount"])
    ]
    if profile["kind"] == "permanent":
        battlefield_sources.extend(new_sources)
    available.extend(new_sources)
    return None


def _is_instant_or_flash(card):
    return "Instant" in card["type_line"] or "flash" in card["oracle_text"].lower()


def _has_x_cost(mana_cost):
    return "{X}" in (mana_cost or "")


def _is_permanent_type(type_line):
    return not ("Instant" in type_line or "Sorcery" in type_line)


FLASHBACK_ENABLERS = {"Snapcaster Mage", "Past in Flames", "Underworld Breach"}
# Sorcery-speed engine pieces with no mana ability of their own, but real
# mechanical impact on the copy trigger -- cast in main phase, before combat,
# so they're online for the rest of the turn (and every turn after).
PRE_COMBAT_ENGINE_PIECES = {"Veyran, Voice of Duality", "Twinning Staff"}


def _cast_priority(card, commander_card):
    """Lower sorts first: commander > mana sources (rituals/rocks/treasure
    makers, which increase what's available) and land-untap spells (Snap,
    Frantic Search, Turnabout -- refunding already-tapped lands is just as
    much a net-mana ritual as adding new mana outright) > flashback/escape
    enablers and copy-multiplier engine pieces (Veyran, Twinning Staff --
    cast main-phase so graveyard rituals are recastable and the copy
    trigger is upgraded before we commit to an X spell) > X spells (dump
    everything into maximizing X, since our no-opponent model doesn't value
    anything "other spells" do anyway) > everything else (only gets
    whatever's left, which after an X spell dumps its mana is usually
    nothing this turn)."""
    if card is commander_card:
        return 0
    if (
        card.get("_mana_profile") or card.get("_untap_lands") or card["name"] == "Turnabout"
        or card["name"] in SAC_LANDS_RAMP_SPELLS
    ):
        return 1
    if card["name"] in FLASHBACK_ENABLERS or card["name"] in PRE_COMBAT_ENGINE_PIECES:
        return 2
    if _has_x_cost(card["mana_cost"]):
        return 3
    return 4


_FIREBENDING_RE = re.compile(r"Firebending (\d+)")


def _parse_firebending(oracle_text):
    """Firebending N: 'Whenever this creature attacks, add N red mana.'
    Returns N, or 0 if the card doesn't have the keyword."""
    match = _FIREBENDING_RE.search(oracle_text)
    return int(match.group(1)) if match else 0


_TRIBAL_COMBAT_DAMAGE_RE = re.compile(
    r"whenever an? (\w+) you control deals combat damage to a player, "
    r"create (a|an|one|two|three|four|five|six|\d+) [^.]*?\btokens?\b",
    re.I,
)


def _parse_tribal_combat_damage_trigger(oracle_text):
    """'Whenever a Griffin you control deals combat damage to a player,
    create a 2/2 white Griffin creature token with flying.' style triggers
    -- returns {"tribe": "Griffin", "tokens": 1} or None. Only meaningful
    when a profile's win_tribal_token_type turns on combat simulation."""
    match = _TRIBAL_COMBAT_DAMAGE_RE.search(oracle_text)
    if not match:
        return None
    return {"tribe": match.group(1), "tokens": _word_to_number(match.group(2).lower())}


# Real wording shared verbatim by every "token doubler" printed so far
# (Anointed Procession, Mondrak, Elspeth Storm Slayer, Exalted Sunborn) --
# generic and safe to auto-detect rather than listing by name. Multiple
# doublers stack multiplicatively (2**n), matching how stacked replacement
# effects actually resolve.
_TOKEN_DOUBLER_RE = re.compile(r"twice that many.*tokens?.*instead", re.I)

# "Creatures you control are every creature type" (Maskwood Nexus) --
# effectively makes every creature you control a Changeling for tribal
# trigger purposes.
_ALL_CREATURE_TYPES_RE = re.compile(r"creatures you control are every creature type", re.I)


def _is_tribal_type(card, tribe, changeling_active):
    """Whether `card` counts as `tribe` for combat-trigger purposes: it
    literally has that creature type, it has Changeling itself, or a
    Maskwood-Nexus-style effect is making everything every type."""
    if changeling_active or "changeling" in card["oracle_text"].lower():
        return True
    return tribe.lower() in card["type_line"].lower()


# Small lookup table (mirrors _CARD_OVERRIDES's precedent) for "draw a card
# when a small creature you control enters" static abilities -- these react
# to OTHER permanents entering, which doesn't fit the existing "draw on cast"
# profile (resolve_draw_profile) at all. "condition" is checked against a
# creature token's printed power/toughness/mana value (Cathars' Crusade-style
# +1/+1 counters are NOT tracked -- documented simplification, see plan/
# README). "cost" is generic mana optionally paid from that turn's leftover
# floating pool; "once_per_turn" caps it to one trigger regardless of how
# many creatures entered.
ETB_DRAW_TRIGGERS = {
    "Mentor of the Meek": {"condition": "power_le_2", "cost": 1, "once_per_turn": False},
    "Welcoming Vampire": {"condition": "power_le_2", "cost": 0, "once_per_turn": True},
    "Enduring Innocence": {"condition": "power_le_2", "cost": 0, "once_per_turn": True},
    "Symmetry Matrix": {"condition": "power_eq_toughness", "cost": 1, "once_per_turn": False},
    "Tocasia's Welcome": {"condition": "mv_le_3", "cost": 0, "once_per_turn": True},
}


# ---------------------------------------------------------------------------
# Land-sacrifice/recursion engine (Titania, Protector of Argoth-style decks)
# -- entirely opt-in via a profile's win_creature_count_threshold. Every
# other deck still plays exactly one land per turn from hand, as before.
# ---------------------------------------------------------------------------

_LAND_SAC_TOKEN_RE = re.compile(
    r"whenever (?:a land you control is put into a graveyard from the battlefield|you sacrifice a land), "
    r"create (?:a|an|one|two|three|\d+) (?:tapped )?(\d+)/(\d+) \w+ (\w+) creature token",
    re.I,
)


def _parse_land_sacrificed_token_trigger(oracle_text):
    """Titania ('whenever a land you control is put into a graveyard from
    the battlefield, create a 5/3 green Elemental creature token') and
    Baloth Prime ('whenever you sacrifice a land, create a tapped 4/4 green
    Beast creature token...') style triggers -- returns
    {"power": 5, "toughness": 3, "type": "Elemental"} or None."""
    match = _LAND_SAC_TOKEN_RE.search(oracle_text)
    if not match:
        return None
    return {"power": int(match.group(1)), "toughness": int(match.group(2)), "type": match.group(3)}


_PLAY_LANDS_FROM_GRAVEYARD_RE = re.compile(r"you may play lands? from your graveyard", re.I)

_EXTRA_LAND_DROPS_RE = re.compile(
    r"play (a|an|one|two|three|\d+) additional lands? on each of your turns", re.I
)


def _parse_extra_land_drops(oracle_text):
    """Azusa ('play two additional lands') / Icetill Explorer ('play an
    additional land') style abilities -- returns the extra count, or 0."""
    match = _EXTRA_LAND_DROPS_RE.search(oracle_text)
    return _word_to_number(match.group(1).lower()) if match else 0


# Name-keyed (mirrors ETB_DRAW_TRIGGERS's precedent) since the "sacrifice N
# lands, search M basics" shape varies too much per-card for one clean
# regex. (lands_sacrificed, basics_fetched) -- "all" means "as many as you
# currently control." Entish Restoration's "3 instead of 2 with a power-4+
# creature" is a runtime check (see _run_sac_lands_ramp_spell); Crop
# Rotation's "any land card" is approximated as a basic, same simplification
# as everywhere else in this file; "enters tapped" is ignored, consistent
# with the file's existing "lands always enter untapped" simplification.
SAC_LANDS_RAMP_SPELLS = {
    "Harrow": (1, 2),
    "Crop Rotation": (1, 1),
    "Entish Restoration": (1, 2),
    "Roiling Regrowth": (1, 2),
    "Cycle of Renewal": (1, 2),
    "Planar Engineering": (2, 4),
    "Scapeshift": ("all", "all"),
}


def _make_token_card(type_name, power, toughness):
    """Synthetic card dict for a land-sac-triggered token (Titania's 5/3
    Elemental, Baloth Prime's 4/4 Beast, ...) -- shaped like a real card
    dict so it works everywhere one is expected (battlefield_permanent_cards,
    frame capture, ETB-trigger/power checks)."""
    return {
        "name": type_name,
        "type_line": f"Creature Token — {type_name}",
        "is_land": False,
        "mana_cost": "",
        "cmc": 0,
        "oracle_text": "",
        "color_identity": [],
        "produced_mana": None,
        "image_url": None,
        "power": power,
        "toughness": toughness,
        "_mana_profile": None,
        "_draw_profile": None,
        "_untap_lands": None,
    }


def _sacrifice_land(land_card, battlefield_lands, graveyard_lands, battlefield_permanents,
                     battlefield_permanent_cards, battlefield_creatures, turn):
    """A land the engine controls is put into the graveyard from the
    battlefield (a fetch/ETB-search land resolving, or an additional cost
    on a ramp spell). Removes it from battlefield_lands, banks it in
    graveyard_lands for recursion (Ramunap Excavator/Crucible of
    Worlds/...), and fires every land-sac token trigger found on ANY
    battlefield permanent (not just the commander -- Baloth Prime needs
    this too), each doubled by token-doubler permanents in play (same
    _TOKEN_DOUBLER_RE used for Zeriam's tribal tokens). Mutates
    battlefield_lands/graveyard_lands/battlefield_permanents/
    battlefield_permanent_cards/battlefield_creatures in place."""
    if land_card["name"] in battlefield_lands:
        battlefield_lands.remove(land_card["name"])
    graveyard_lands.append(land_card)

    num_doublers = sum(1 for c in battlefield_permanent_cards if _TOKEN_DOUBLER_RE.search(c["oracle_text"]))
    doubler_multiplier = 2 ** num_doublers
    for source_card in list(battlefield_permanent_cards):
        trigger = _parse_land_sacrificed_token_trigger(source_card["oracle_text"])
        if not trigger:
            continue
        token_card = _make_token_card(trigger["type"], trigger["power"], trigger["toughness"])
        for _ in range(doubler_multiplier):
            battlefield_permanents.append(trigger["type"])
            battlefield_permanent_cards.append(token_card)
            battlefield_creatures.append(
                {"name": trigger["type"], "card": token_card, "entered_turn": turn, "has_double_strike": False}
            )


def _search_basic_lands(library, count, battlefield_sources):
    """Pulls up to `count` basic land cards out of `library` (mutated in
    place, same as a real search), preferring colors not yet covered by
    battlefield_sources. Filtering against `library` -- rather than a
    separate "deck colors" concept -- naturally never picks a color the
    deck doesn't run, since a card of that color simply won't be in the
    library to find. Returns the cards found (fewer than `count` if the
    library's run dry on basics)."""
    found = []
    for _ in range(count):
        basics = [c for c in library if c["name"] in BASIC_LAND_BY_COLOR.values()]
        if not basics:
            break
        have = {clr for src in battlefield_sources for clr in src["colors"]}
        have |= {
            clr for f in found for clr, name in BASIC_LAND_BY_COLOR.items() if name == f["name"]
        }
        preferred = [
            c for c in basics
            if next((clr for clr, name in BASIC_LAND_BY_COLOR.items() if name == c["name"]), None) not in have
        ]
        pick = preferred[0] if preferred else basics[0]
        library.remove(pick)
        found.append(pick)
    return found


def _least_valuable_land_to_sacrifice(battlefield_lands, battlefield_sources):
    """Picks a land name to sacrifice as an additional cost (Harrow-style
    ramp spells): the one contributing the fewest colors not already
    covered by the rest of the board, mirroring _choose_discard's
    'least useful' heuristic. Returns a name, or None if there are no
    lands in play."""
    if not battlefield_lands:
        return None
    all_colors = {}
    for name in battlefield_lands:
        source = next((s for s in battlefield_sources if s["name"] == name), None)
        all_colors[name] = source["colors"] if source else frozenset()

    def unique_color_count(name):
        others = set().union(*(c for n, c in all_colors.items() if n != name)) if len(all_colors) > 1 else set()
        return len(all_colors[name] - others)

    return min(battlefield_lands, key=unique_color_count)


def _matches_hold_tag(card, tag):
    tag_lower = tag.lower()
    return tag_lower in card["type_line"].lower() or tag_lower in card["oracle_text"].lower()


def _is_instant_or_sorcery(card):
    return "Instant" in card["type_line"] or "Sorcery" in card["type_line"]


def _graveyard_candidates(graveyard_cards, flashback_mode, flashback_uses_left):
    """Which graveyard cards can be (re)cast this turn, per whichever
    enabler is currently active: Snapcaster Mage ("single" -- one
    Instant/Sorcery, consumes a use), Past in Flames ("all" -- unlimited
    Instant/Sorcery), or Underworld Breach ("escape" -- any nonland card,
    but only worth it with 4+ cards in the yard since escape also costs
    exiling 3 OTHERS)."""
    if flashback_mode == "single" and flashback_uses_left > 0:
        return [c for c in graveyard_cards if not c["is_land"] and _is_instant_or_sorcery(c)]
    if flashback_mode == "all":
        return [c for c in graveyard_cards if not c["is_land"] and _is_instant_or_sorcery(c)]
    if flashback_mode == "escape" and len(graveyard_cards) >= 4:
        return [c for c in graveyard_cards if not c["is_land"]]
    return []


def _compute_copy_multiplier(card, is_attacking, copies_while_attacking, is_commander_cast, battlefield_permanents):
    """How many total times this cast resolves (1 = just the original, no
    copying). The attack trigger provides one copy (multiplier 2); Veyran,
    Voice of Duality doubles that again for Instant/Sorcery spells
    specifically (its Magecraft trigger doubling), and Twinning Staff
    doubles again for anything ("copy it that many times plus one more,"
    stacking multiplicatively with any other doubling already in effect)."""
    if not (is_attacking and copies_while_attacking and not is_commander_cast and _is_instant_or_flash(card)):
        return 1
    multiplier = 2
    perm_names = {p.replace(" (copy)", "") for p in battlefield_permanents}
    if "Veyran, Voice of Duality" in perm_names and ("Instant" in card["type_line"] or "Sorcery" in card["type_line"]):
        multiplier *= 2
    if "Twinning Staff" in perm_names:
        multiplier *= 2
    return multiplier


def _three_steps_ahead_cost_and_target(battlefield_permanent_cards):
    """Three Steps Ahead is a Spree instant: base {U}, plus optional modes.
    We always take the "draw two, discard one" mode (+{2}), and additionally
    the "copy target artifact" mode (+{3}) if we control a non-legendary
    artifact to copy -- picking the highest-CMC one. Returns
    (effective_mana_cost, target_card_or_None)."""
    candidates = [
        c for c in battlefield_permanent_cards
        if "Artifact" in c["type_line"] and "Legendary" not in c["type_line"]
    ]
    if candidates:
        target = max(candidates, key=lambda c: c["cmc"])
        return "{U}{3}{2}", target
    return "{U}{2}", None


WIN_X_THRESHOLD = 8  # casting an X spell for this much or more counts as a "win"
# X spells that don't actually deal damage to a player -- removal
# (Shellshock, Street Spasm only ever hit creatures) or pure utility
# (Reality Spasm just taps/untaps permanents, no damage at all) -- so a
# big X on these shouldn't count as "winning" the game.
WIN_EXCLUDED_CARDS = {"Shellshock", "Street Spasm", "Reality Spasm"}


# ---------------------------------------------------------------------------
# Tribal combat + token win condition (Zeriam, Golden Wind-style decks) --
# entirely opt-in via a profile's win_tribal_token_type. Every other deck
# never touches any of this.
# ---------------------------------------------------------------------------

def _make_tribal_token_card(tribe):
    """Synthetic card dict for a combat-created tribal token (e.g. Zeriam's
    2/2 Griffin), shaped like a real card dict so it works everywhere a
    parsed Scryfall card would (battlefield_permanent_cards, frame capture,
    ETB-trigger checks)."""
    return {
        "name": tribe,
        "type_line": f"Creature Token — {tribe}",
        "is_land": False,
        "mana_cost": "",
        "cmc": 0,
        "oracle_text": "Flying",
        "color_identity": [],
        "produced_mana": None,
        "image_url": None,
        "power": 2,
        "toughness": 2,
        "_mana_profile": None,
        "_draw_profile": None,
        "_untap_lands": None,
    }


def _etb_condition_met(entering_card, condition):
    power, toughness = entering_card.get("power"), entering_card.get("toughness")
    if condition == "power_le_2":
        return power is not None and power <= 2
    if condition == "power_eq_toughness":
        return power is not None and toughness is not None and power == toughness
    if condition == "mv_le_3":
        return entering_card.get("cmc", 0) <= 3
    return False


def _fire_etb_draw_triggers(entering_card, battlefield_permanents, battlefield_permanent_cards,
                             hand, library, available, etb_fired_this_turn, rumor_gatherer_state):
    """Fires "whenever another creature you control [with some condition]
    enters, draw a card" static triggers (ETB_DRAW_TRIGGERS) plus Rumor
    Gatherer's "second creature ETB this turn draws instead of scries"
    variant, for ONE entering creature. Mutates hand/library/available/
    etb_fired_this_turn/rumor_gatherer_state in place; draws are applied
    directly (no separate frame bookkeeping -- these show up the same as
    any other card in hand)."""
    for source_name, source_card in zip(battlefield_permanents, battlefield_permanent_cards):
        if source_card is entering_card:
            continue  # "another creature" -- can't trigger off its own ETB
        base_name = source_name.replace(" (copy)", "")
        rule = ETB_DRAW_TRIGGERS.get(base_name)
        if rule:
            if not _etb_condition_met(entering_card, rule["condition"]):
                continue
            if rule["once_per_turn"] and source_name in etb_fired_this_turn:
                continue
            if rule["cost"] > 0:
                if len(available) < rule["cost"]:
                    continue
                del available[:rule["cost"]]
            if library:
                hand.append(library.pop(0))
            if rule["once_per_turn"]:
                etb_fired_this_turn.add(source_name)
        elif base_name == "Rumor Gatherer":
            rumor_gatherer_state["count"] += 1
            if rumor_gatherer_state["count"] == 2 and library:
                hand.append(library.pop(0))


def _run_combat_step(profile, turn, commander_card, battlefield_creatures, battlefield_permanents,
                      battlefield_permanent_cards, available, hand, library, cast_this_turn_names,
                      tribal_token_count, tribal_trigger, rng, etb_fired_this_turn, rumor_gatherer_state):
    """Simulates one turn's combat for a tribal-combat-token win condition:
    every creature without summoning sickness attacks unopposed (consistent
    with the engine's existing no-opponent/no-blockers model). Whichever of
    the profile's double-strike sources are in play determine how many
    creatures have double strike this combat (see DEFAULT_PROFILE);
    double-struck tribal attackers trigger the tribal combat-damage ability
    twice instead of once. Roaming Throne and token-doubler permanents then
    multiply the resulting token count. Returns
    (new_tribal_token_count, tokens_created_this_combat) -- mutates
    battlefield_creatures/battlefield_permanents/battlefield_permanent_cards/
    hand/library in place."""
    tribe = profile.get("win_tribal_token_type")
    if not tribe or tribal_trigger is None:
        return tribal_token_count, 0

    perm_names = {p.replace(" (copy)", "") for p in battlefield_permanents}
    changeling_active = any(_ALL_CREATURE_TYPES_RE.search(c["oracle_text"]) for c in battlefield_permanent_cards)

    commander_entry = next(
        (c for c in battlefield_creatures if commander_card is not None and c["card"] is commander_card), None
    )

    # 1. Double strike assignment for this combat.
    if commander_entry and perm_names & set(profile.get("double_strike_auto_commander_cards", [])):
        commander_entry["has_double_strike"] = True

    any_has_double_strike = any(c["has_double_strike"] for c in battlefield_creatures)
    blanket_cards = set(profile.get("double_strike_blanket_cards", []))
    blanket_active = bool(perm_names & blanket_cards) or bool(cast_this_turn_names & blanket_cards)
    propagator_active = bool(perm_names & set(profile.get("double_strike_propagator_cards", []))) and any_has_double_strike
    blanket_this_combat = blanket_active or propagator_active

    if not blanket_this_combat and (perm_names & set(profile.get("double_strike_single_target_cards", []))):
        if commander_entry and not commander_entry["has_double_strike"]:
            commander_entry["has_double_strike"] = True
        else:
            candidates = [
                c for c in battlefield_creatures
                if not c["has_double_strike"] and _is_tribal_type(c["card"], tribe, changeling_active)
            ]
            if candidates:
                rng.choice(candidates)["has_double_strike"] = True

    # 2. Attackers (no summoning sickness) and their trigger counts.
    total_base_triggers = 0
    for creature in battlefield_creatures:
        if creature["entered_turn"] >= turn:
            continue  # summoning sick, can't attack this turn
        if not _is_tribal_type(creature["card"], tribe, changeling_active):
            continue
        has_double_strike = creature["has_double_strike"] or blanket_this_combat
        total_base_triggers += 2 if has_double_strike else 1

    if total_base_triggers == 0:
        return tribal_token_count, 0

    # 3. Roaming Throne doubles the tribal trigger itself, if it's tuned to this tribe.
    roaming_throne_multiplier = 1
    if "Roaming Throne" in perm_names and (profile.get("roaming_throne_chosen_type") or "").lower() == tribe.lower():
        roaming_throne_multiplier = 2

    # 4. Token doublers stack multiplicatively (real replacement-effect rules).
    num_doublers = sum(1 for c in battlefield_permanent_cards if _TOKEN_DOUBLER_RE.search(c["oracle_text"]))

    new_tokens = total_base_triggers * roaming_throne_multiplier * tribal_trigger["tokens"] * (2 ** num_doublers)

    # 5. Create the tokens, fire their ETB draw triggers.
    token_card = _make_tribal_token_card(tribe)
    for _ in range(new_tokens):
        battlefield_permanents.append(tribe)
        battlefield_permanent_cards.append(token_card)
        battlefield_creatures.append(
            {"name": tribe, "card": token_card, "entered_turn": turn, "has_double_strike": False}
        )
        _fire_etb_draw_triggers(
            token_card, battlefield_permanents, battlefield_permanent_cards,
            hand, library, available, etb_fired_this_turn, rumor_gatherer_state
        )

    # 6. Bennie Bracks: draw at end step if a token was created this turn.
    if "Bennie Bracks, Zoologist" in perm_names and library:
        hand.append(library.pop(0))

    return tribal_token_count + new_tokens, new_tokens


# ---------------------------------------------------------------------------
# Upkeep-reveal-until-creature-or-planeswalker engine (The Prismatic Bridge-
# style decks) -- entirely self-gating: it only ever fires if a permanent
# bearing this exact ability is actually in play, so no profile flag is
# needed to turn it on for decks that don't run one.
# ---------------------------------------------------------------------------

_UPKEEP_REVEAL_UNTIL_RE = re.compile(
    r"at the beginning of your upkeep, reveal cards from the top of your library "
    r"until you reveal a creature or planeswalker card\. put that card onto the battlefield",
    re.I,
)


def _has_upkeep_reveal_trigger(battlefield_permanent_cards):
    return any(_UPKEEP_REVEAL_UNTIL_RE.search(c["oracle_text"]) for c in battlefield_permanent_cards)


def _resolve_upkeep_reveal(library):
    """Reveals from the top of `library` (mutated in place) until a
    Creature or Planeswalker card, puts it into play (returned), and moves
    everything revealed along the way to the bottom. `library` is already a
    randomly shuffled list by construction, so appending the misses
    satisfies "the rest in a random order" without a fresh shuffle. Returns
    None if the library runs out first."""
    misses = []
    found = None
    while library:
        card = library.pop(0)
        if "Creature" in card["type_line"] or "Planeswalker" in card["type_line"]:
            found = card
            break
        misses.append(card)
    library.extend(misses)
    return found


# Name-keyed (mirrors SAC_LANDS_RAMP_SPELLS/ETB_DRAW_TRIGGERS's precedent)
# since each "copy target triggered ability you control" source has a
# genuinely different cost/multiplier/restriction shape. "generic"/"colored"
# describe a mana cost paid via the same try_pay() helper used everywhere
# else; "tap_other_creatures" is a nonmana alt-cost (Kirol); "max_uses" is a
# lifetime cap across the whole game (Peter Parker's Camera's film
# counters); "is_creature" gates on summoning sickness. Gogo (scales with
# however much mana is left) and Vantress Visions (a one-shot instant cast
# from hand, not a repeatable permanent) don't fit this shape and are
# special-cased directly in _run_upkeep_phase.
COPY_TRIGGER_SOURCES = {
    "Strionic Resonator": {"generic": 2, "colored": [], "multiplier": 1, "max_uses": None, "is_creature": False},
    "Lithoform Engine": {"generic": 2, "colored": [], "multiplier": 1, "max_uses": None, "is_creature": False},
    "Weaver of Harmony": {"generic": 0, "colored": ["G"], "multiplier": 1, "max_uses": None, "is_creature": True},
    "Adric, Mathematical Genius": {"generic": 2, "colored": ["U"], "multiplier": 1, "max_uses": None, "is_creature": True},
    "Peter Parker's Camera": {"generic": 2, "colored": [], "multiplier": 1, "max_uses": 3, "is_creature": False},
    "Mister Fantastic": {"generic": 0, "colored": ["R", "G", "W", "U"], "multiplier": 2, "max_uses": None, "is_creature": True},
    "Kirol, Attentive First-Year": {"tap_other_creatures": 2, "multiplier": 1, "max_uses": None, "is_creature": True},
}


def _run_upkeep_phase(turn, battlefield_permanents, battlefield_permanent_cards, battlefield_creatures,
                       battlefield_sources, hand, graveyard, graveyard_cards, library,
                       copy_source_uses_remaining):
    """Resolves The Prismatic Bridge's upkeep trigger plus every copy of it
    the board can afford, using only mana from permanents already in play
    at the start of the turn (this turn's land drop hasn't happened yet,
    matching real upkeep timing). Returns (upkeep_available, names_found)
    where upkeep_available is what's left of that mana pool for the rest of
    the turn to use, and names_found lists what got put into play (for
    frame capture). Mutates battlefield_permanents/battlefield_permanent_cards/
    battlefield_creatures/hand/graveyard/graveyard_cards/library in place."""
    upkeep_available = list(battlefield_sources)
    perm_names = {p.replace(" (copy)", "") for p in battlefield_permanents}
    num_activations = 1  # the Bridge's own base trigger

    def _is_unsick_creature(name):
        entry = next((c for c in battlefield_creatures if c["name"] == name), None)
        return entry is not None and entry["entered_turn"] < turn

    for source_name, cfg in COPY_TRIGGER_SOURCES.items():
        if source_name not in perm_names:
            continue
        uses_left = copy_source_uses_remaining.get(source_name, cfg["max_uses"])
        if cfg["max_uses"] is not None and uses_left is not None and uses_left <= 0:
            continue
        if cfg["is_creature"] and not _is_unsick_creature(source_name):
            continue
        if "tap_other_creatures" in cfg:
            others = [c for c in battlefield_creatures if c["name"] != source_name]
            if len(others) < cfg["tap_other_creatures"]:
                continue
        else:
            pips = [{c} for c in cfg["colored"]]
            paid = try_pay(upkeep_available, cfg["generic"], pips)
            if paid is None:
                continue
            upkeep_available = [c for i, c in enumerate(upkeep_available) if i not in paid]
        num_activations += cfg["multiplier"]
        if cfg["max_uses"] is not None:
            copy_source_uses_remaining[source_name] = (uses_left if uses_left is not None else cfg["max_uses"]) - 1

    # Vantress Visions (Virtue of Knowledge's Adventure side) -- a one-shot
    # instant held up and cast in response to the upkeep trigger.
    vantress = next((c for c in hand if c["name"] == "Virtue of Knowledge // Vantress Visions"), None)
    if vantress is not None:
        paid = try_pay(upkeep_available, 1, [{"U"}])
        if paid is not None:
            upkeep_available = [c for i, c in enumerate(upkeep_available) if i not in paid]
            hand.remove(vantress)
            graveyard.append(vantress["name"])
            graveyard_cards.append(vantress)
            num_activations += 1

    # Gogo, Master of Mimicry -- {X}{X} to copy X times; goes last and
    # dumps whatever's left, same "go big since it's the last thing this
    # turn" philosophy as the engine's X-spell handling.
    if "Gogo, Master of Mimicry" in perm_names and _is_unsick_creature("Gogo, Master of Mimicry"):
        x_value = len(upkeep_available) // 2
        if x_value > 0:
            upkeep_available = []
            num_activations += x_value

    names_found = []
    for _ in range(num_activations):
        found = _resolve_upkeep_reveal(library)
        if found is None:
            break
        battlefield_permanents.append(found["name"])
        battlefield_permanent_cards.append(found)
        if "Creature" in found["type_line"]:
            battlefield_creatures.append(
                {"name": found["name"], "card": found, "entered_turn": turn, "has_double_strike": False}
            )
        names_found.append(found["name"])

    return upkeep_available, names_found


def simulate_game(deck_cards, commander_card, max_turns, rng, on_the_play=True, profile=None, capture_frames=False):
    """Plays one solitaire game. `deck_cards` is the 99/100-card library
    (each a dict from fetch_card_data, annotated with a precomputed
    '_mana_profile'). `commander_card`, if given, is cast from an
    always-available command zone alongside the hand and given top casting
    priority every turn. `profile` (see DEFAULT_PROFILE) supplies deck-
    specific sequencing rules; falls back to generic defaults if omitted.
    `rng` is a random.Random instance -- pass one seeded by game index so any
    specific game can be deterministically replayed later. If the commander
    has a "Firebending N" ability, we assume it attacks every turn starting
    the turn after it's cast, adding N red mana usable only on Instant/Flash
    spells that turn. Set `capture_frames` to also build a turn-by-turn
    structured replay (land taps, mana floats, board state) for the web
    UI's game player -- skipped by default since most games are only used
    for aggregate stats. Returns (first_cast_turn, frames_or_None, win_turn)
    where win_turn is the first turn an X spell was cast for
    WIN_X_THRESHOLD+, OR (if the profile sets win_tribal_token_type) the
    first turn a combat-created token count crossed
    win_tribal_token_threshold -- None if neither ever happened."""
    profile = profile or DEFAULT_PROFILE
    hold_tags = profile.get("hold_until_commander_resolves", [])
    copies_while_attacking = profile.get("commander_copies_spells_while_attacking", False)
    reserve_kinds = set(profile.get("reserve_mana_kinds_for_x_spells", []))

    library = list(deck_cards)
    hand, library = _draw_opening_hand(library, rng)

    battlefield_sources = []  # list of {"name","colors"}, one per permanent mana source
    battlefield_lands = []  # names of lands in play
    battlefield_permanents = []  # names of nonland permanents (creatures/artifacts/etc.) in play
    battlefield_permanent_cards = []  # full card dicts, parallel to battlefield_permanents
    battlefield_costed_rocks = []  # mana rocks with a per-use activation cost (Signets, etc.)
    graveyard = []  # names of resolved Instants/Sorceries and discarded cards, in order
    graveyard_cards = []  # full card dicts, parallel to graveyard -- for flashback/escape recasting
    first_cast_turn = {}
    frames = [] if capture_frames else None
    win_turn = None
    win_spell_name = None
    win_x_value = None
    win_label = None
    commander_cast_turn = None
    firebending_amount = (commander_card or {}).get("_firebending", 0)
    treasure_count = 0  # banked across turns
    treasure_colors = set()

    # Tribal-combat-token win condition (see DEFAULT_PROFILE) -- a complete
    # no-op for decks whose profile doesn't set win_tribal_token_type.
    battlefield_creatures = []  # [{"name","card","entered_turn","has_double_strike"}], creatures only
    win_tribal_type = profile.get("win_tribal_token_type")
    win_tribal_threshold = profile.get("win_tribal_token_threshold")
    tribal_trigger = (
        _parse_tribal_combat_damage_trigger((commander_card or {}).get("oracle_text", ""))
        if win_tribal_type else None
    )
    tribal_token_count = 0  # cumulative tokens of win_tribal_type created via combat

    # Land-sacrifice/recursion win condition (see DEFAULT_PROFILE) -- a
    # complete no-op for decks whose profile doesn't set
    # win_creature_count_threshold; every other deck still plays exactly
    # one land per turn from hand, as before.
    graveyard_lands = []  # land card dicts sacrificed and eligible for recursion
    battlefield_land_cards = {}  # land name -> card dict, for sac-to-ramp spells to look up what they're sacrificing
    win_creature_threshold = profile.get("win_creature_count_threshold")
    win_creature_min_cmc = profile.get("win_creature_min_cmc")
    copy_source_uses_remaining = {}  # persists across turns -- Peter Parker's Camera's lifetime 3-use cap

    for turn in range(1, max_turns + 1):
        drew_card = None
        land_events = []
        cast_events = []
        activated_rock_names = []
        cast_this_turn_names = set()
        etb_fired_this_turn = set()
        rumor_gatherer_state = {"count": 0}

        # The Prismatic Bridge-style upkeep trigger (see DEFAULT_PROFILE
        # notes near win_creature_min_cmc) -- happens before the draw step,
        # using only mana from permanents already in play (this turn's land
        # drop hasn't happened yet). upkeep_leftover_mana threads whatever's
        # left into the casting loop's mana pool below.
        upkeep_source_count = len(battlefield_sources)
        upkeep_leftover_mana = None
        upkeep_hits = []
        if _has_upkeep_reveal_trigger(battlefield_permanent_cards):
            upkeep_leftover_mana, upkeep_hits = _run_upkeep_phase(
                turn, battlefield_permanents, battlefield_permanent_cards, battlefield_creatures,
                battlefield_sources, hand, graveyard, graveyard_cards, library, copy_source_uses_remaining
            )

        if not (turn == 1 and on_the_play) and library:
            drawn = library.pop(0)
            hand.append(drawn)
            drew_card = drawn["name"]

        if win_creature_threshold:
            # Multiple land drops per turn (Azusa/Icetill Explorer),
            # replaying fetch/search lands out of graveyard_lands (Ramunap
            # Excavator/Crucible of Worlds/Conduit of Worlds/...) -- see
            # DEFAULT_PROFILE.
            max_land_drops = 1 + sum(
                _parse_extra_land_drops(c["oracle_text"]) for c in battlefield_permanent_cards
            )
            can_recur_lands = any(
                _PLAY_LANDS_FROM_GRAVEYARD_RE.search(c["oracle_text"]) for c in battlefield_permanent_cards
            )
            for _ in range(max_land_drops):
                hand_lands = [c for c in hand if c["is_land"]]
                candidates = hand_lands + (graveyard_lands if can_recur_lands else [])
                if not candidates:
                    break
                # Always crack whatever's crackable first -- that's the
                # deck's actual optimal line (more Titania/Baloth Prime
                # triggers), falling back to the normal color-fixing pick.
                fetch_candidates = [
                    c for c in candidates if c["_mana_profile"] and c["_mana_profile"]["is_fetch"]
                ]
                land = _choose_land_to_play(fetch_candidates or candidates, battlefield_sources)
                from_graveyard = any(land is g for g in graveyard_lands)
                if from_graveyard:
                    graveyard_lands.remove(land)
                else:
                    hand.remove(land)
                battlefield_lands.append(land["name"])
                battlefield_land_cards[land["name"]] = land
                mana_profile = land["_mana_profile"]
                fetched_color = None
                if mana_profile:
                    fetched_color = _apply_mana_source(mana_profile, battlefield_sources, [], hand, land["name"])
                    if mana_profile["is_fetch"]:
                        if fetched_color:
                            # _apply_mana_source names the found mana source
                            # after the fetchland itself (see its "source" dict)
                            # -- match that here so battlefield_lands and
                            # battlefield_sources agree on what's actually in
                            # play (needed for tapped-chip highlighting and
                            # _least_valuable_land_to_sacrifice's lookups).
                            battlefield_lands.append(land["name"])
                        _sacrifice_land(
                            land, battlefield_lands, graveyard_lands, battlefield_permanents,
                            battlefield_permanent_cards, battlefield_creatures, turn
                        )
                land_events.append({"name": land["name"], "fetched": fetched_color, "from_graveyard": from_graveyard})
        else:
            hand_lands = [c for c in hand if c["is_land"]]
            if hand_lands:
                land = _choose_land_to_play(hand_lands, battlefield_sources)
                hand.remove(land)
                battlefield_lands.append(land["name"])
                mana_profile = land["_mana_profile"]
                fetched_color = None
                if mana_profile:
                    fetched_color = _apply_mana_source(mana_profile, battlefield_sources, [], hand, land["name"])
                land_events.append({"name": land["name"], "fetched": fetched_color, "from_graveyard": False})

        if upkeep_leftover_mana is not None:
            # Leftover upkeep-time mana plus whatever new sources this
            # turn's land drop(s) added (battlefield_sources is append-only
            # within a turn, so this slice is safe).
            available = upkeep_leftover_mana + battlefield_sources[upkeep_source_count:]
        else:
            available = list(battlefield_sources)
        available, activated_now = _activate_costed_rocks(battlefield_costed_rocks, available)
        activated_rock_names.extend(activated_now)
        flashback_mode = None  # "single" (Snapcaster) | "all" (Past in Flames) | "escape" (Underworld Breach)
        flashback_uses_left = 0
        commander_tapped_for_mana = False  # Firebending mana used this turn -- show her as tapped too
        is_attacking = commander_cast_turn is not None and turn > commander_cast_turn
        # Named exactly after the commander (not "... (attacking)") so the
        # Game Player's tap highlight lands on her existing permanent chip
        # instead of a separate untapped-looking entry.
        firebending_source_name = commander_card["name"] if commander_card else "Attack trigger"
        flash_mana = [
            {"name": firebending_source_name, "colors": frozenset({"R"})}
            for _ in range(firebending_amount)
        ] if is_attacking else []

        # Hold whatever this deck's profile says to hold until the
        # commander has resolved (e.g. Instants/Flash worth more once it's
        # out attacking and copying them).
        commander_is_out = commander_cast_turn is not None
        pool = [
            c for c in hand
            if not c["is_land"]
            and (commander_is_out or not any(_matches_hold_tag(c, tag) for tag in hold_tags))
            # Underworld Breach's escape also costs exiling 3 OTHER
            # graveyard cards -- not worth casting without enough there.
            and (c["name"] != "Underworld Breach" or len(graveyard_cards) >= 4)
            # Harrow-style ramp spells require a land to sacrifice as an
            # additional cost -- nothing to sac, nothing to cast.
            and (c["name"] not in SAC_LANDS_RAMP_SPELLS or battlefield_lands)
        ]
        if commander_card is not None and commander_cast_turn is None:
            pool.append(commander_card)

        progressed = True
        while progressed:
            progressed = False
            candidates = sorted(
                pool + _graveyard_candidates(graveyard_cards, flashback_mode, flashback_uses_left),
                key=lambda c: (_cast_priority(c, commander_card), -c["cmc"]),
            )
            for card in candidates:
                three_steps_target = None
                effective_mana_cost = card["mana_cost"]
                if card["name"] == "Three Steps Ahead":
                    effective_mana_cost, three_steps_target = _three_steps_ahead_cost_and_target(battlefield_permanent_cards)

                generic, pips = parse_mana_cost(effective_mana_cost)
                has_x = _has_x_cost(effective_mana_cost)
                flash_eligible = bool(flash_mana) and _is_instant_or_flash(card)

                # Treasures are always banked (they're real permanents), but
                # only spendable here if the profile doesn't reserve them
                # for X spells specifically -- if it does, they only enter
                # the pool once we reach an X spell.
                treasure_reserved = "treasure" in reserve_kinds
                treasures_spendable = treasure_count and (has_x or not treasure_reserved)
                combined = list(available)
                if flash_eligible:
                    combined += flash_mana
                if treasures_spendable:
                    combined += [
                        {"name": "Treasure", "colors": frozenset(treasure_colors or {"C"})}
                        for _ in range(treasure_count)
                    ]

                used = try_pay(combined, generic, pips)
                if used is None:
                    continue
                tapped_sources = [combined[i] for i in used]

                n_avail = len(available)
                n_flash = len(flash_mana) if flash_eligible else 0
                avail_used = {i for i in used if i < n_avail}
                flash_used = {i - n_avail for i in used if n_avail <= i < n_avail + n_flash}
                treasures_used = sum(1 for i in used if i >= n_avail + n_flash)

                available = [c for i, c in enumerate(available) if i not in avail_used]
                if flash_eligible:
                    if flash_used:
                        commander_tapped_for_mana = True
                    flash_mana = [c for i, c in enumerate(flash_mana) if i not in flash_used]
                treasure_count -= treasures_used

                x_value = None
                if has_x:
                    # Dump everything left (lands, attack mana, banked
                    # treasures) into X -- go big since it's the last spell.
                    if flash_mana:
                        commander_tapped_for_mana = True
                    x_value = len(available) + len(flash_mana) + treasure_count
                    available = []
                    flash_mana = []
                    treasure_count = 0
                    if win_turn is None and x_value >= WIN_X_THRESHOLD and card["name"] not in WIN_EXCLUDED_CARDS:
                        win_turn = turn
                        win_spell_name = card["name"]
                        win_x_value = x_value
                        win_label = f"cast {card['name']} for X={x_value}!"

                is_commander_cast = commander_card is not None and card is commander_card
                from_graveyard = any(card is g for g in graveyard_cards)
                if is_commander_cast:
                    commander_cast_turn = turn
                elif from_graveyard:
                    # Cast from the graveyard via flashback/escape -- the
                    # card gets exiled after resolving, not returned to hand
                    # or the graveyard.
                    graveyard_cards.remove(card)
                    graveyard.remove(card["name"])
                    if flashback_mode == "single":
                        flashback_uses_left -= 1
                    elif flashback_mode == "escape":
                        for _ in range(min(3, len(graveyard_cards))):
                            exiled = graveyard_cards.pop(0)
                            graveyard.remove(exiled["name"])
                else:
                    hand.remove(card)
                    pool.remove(card)

                if card["name"] not in first_cast_turn:
                    first_cast_turn[card["name"]] = turn
                cast_this_turn_names.add(card["name"])

                # How many total times this resolves: 1 = no copying, 2+ if
                # the commander's attack trigger (and Veyran/Twinning Staff
                # doubling it further) applies. Only Instant-speed spells
                # can be cast "while attacking" (during combat) at all.
                copy_multiplier = _compute_copy_multiplier(
                    card, is_attacking, copies_while_attacking, is_commander_cast, battlefield_permanents
                )
                extra_copies = copy_multiplier - 1
                is_copied = extra_copies > 0

                permanents_added = 0
                # NOT "is_commander_cast or ..." -- a commander that's a
                # noncreature permanent (e.g. an MDFC commander cast as its
                # Enchantment back face) shouldn't get counted as a creature
                # just for being the commander.
                is_creature_permanent = "Creature" in card["type_line"]
                new_creature_entries = []
                if is_commander_cast or _is_permanent_type(card["type_line"]):
                    battlefield_permanents.append(card["name"])
                    battlefield_permanent_cards.append(card)
                    permanents_added = 1
                    if is_creature_permanent:
                        entry = {"name": card["name"], "card": card, "entered_turn": turn, "has_double_strike": False}
                        battlefield_creatures.append(entry)
                        new_creature_entries.append(entry)
                    # The legend rule: extra copies of a Legendary permanent
                    # get sacrificed immediately, so only one ever sticks
                    # around no matter how many times it was copied.
                    if "Legendary" not in card["type_line"]:
                        for _ in range(extra_copies):
                            battlefield_permanents.append(card["name"] + " (copy)")
                            battlefield_permanent_cards.append(card)
                            permanents_added += 1
                            if is_creature_permanent:
                                entry = {"name": card["name"] + " (copy)", "card": card,
                                         "entered_turn": turn, "has_double_strike": False}
                                battlefield_creatures.append(entry)
                                new_creature_entries.append(entry)
                elif not from_graveyard:
                    graveyard.append(card["name"])  # Instants/Sorceries go to the graveyard once resolved
                    graveyard_cards.append(card)

                if win_tribal_type:
                    for _ in new_creature_entries:
                        _fire_etb_draw_triggers(
                            card, battlefield_permanents, battlefield_permanent_cards,
                            hand, library, available, etb_fired_this_turn, rumor_gatherer_state
                        )

                mana_profile = card.get("_mana_profile")
                if mana_profile:
                    if mana_profile["kind"] == "treasure":
                        treasure_count += mana_profile["amount"] * copy_multiplier
                        treasure_colors = mana_profile["produces"]
                    elif mana_profile["kind"] == "permanent" and mana_profile.get("activation_cost", 0) > 0:
                        # Signet-style: costs mana every time it's tapped,
                        # so it's not a free ongoing source -- pay for it
                        # right away (same-turn benefit) and again each
                        # future turn via _activate_costed_rocks.
                        for _ in range(copy_multiplier):
                            rock = dict(mana_profile, source_name=card["name"])
                            battlefield_costed_rocks.append(rock)
                            available, activated_now = _activate_costed_rocks([rock], available)
                            activated_rock_names.extend(activated_now)
                    else:
                        for _ in range(copy_multiplier):
                            _apply_mana_source(mana_profile, battlefield_sources, available, hand, card["name"])

                drew = discarded = None
                draw_profile = card.get("_draw_profile")
                if draw_profile:
                    if draw_profile["discard"]:
                        discarded_cards = _discard_cards(hand, draw_profile["discard"], battlefield_sources)
                        pool = [c for c in pool if not any(c is d for d in discarded_cards)]
                        discarded = len(discarded_cards)
                        graveyard.extend(d["name"] for d in discarded_cards)
                        graveyard_cards.extend(discarded_cards)
                    drew = draw_profile["draw"] * copy_multiplier
                    for _ in range(drew):
                        if library:
                            hand.append(library.pop(0))

                # A handful of cards do something the generic mana/draw
                # models can't express -- refunding already-tapped lands.
                untap_n = card.get("_untap_lands")
                if untap_n:
                    _refund_lands(untap_n * copy_multiplier, battlefield_sources, battlefield_lands, available)
                if card["name"] == "Turnabout":
                    _refund_lands(len(battlefield_lands), battlefield_sources, battlefield_lands, available)
                if card["name"] == "Reality Spasm" and x_value:
                    _refund_lands(x_value, battlefield_sources, battlefield_lands, available)
                if card["name"] == "Bottle-Cap Blast":
                    treasure_count += rng.randint(2, 4) * copy_multiplier
                    treasure_colors = set(ALL_COLORS)
                if card["name"] == "The Last Agni Kai":
                    amount = rng.randint(2, 3) * copy_multiplier
                    available.extend({"name": card["name"], "colors": frozenset({"R"})} for _ in range(amount))
                if card["name"] == "Three Steps Ahead" and three_steps_target is not None:
                    battlefield_permanents.append(three_steps_target["name"] + " (copy)")
                    battlefield_permanent_cards.append(three_steps_target)
                    permanents_added += 1
                if card["name"] == "Brotherhood Regalia" and available:
                    # Equip legendary creature {1} -- assume we always have
                    # a legendary creature (the commander) to attach it to.
                    available = available[1:]
                ramp_spec = SAC_LANDS_RAMP_SPELLS.get(card["name"])
                if ramp_spec and win_creature_threshold:
                    sac_count, fetch_count = ramp_spec
                    if sac_count == "all":
                        sac_count = fetch_count = len(battlefield_lands)
                    if card["name"] == "Entish Restoration" and any(
                        (c["card"].get("power") or 0) >= 4 for c in battlefield_creatures
                    ):
                        fetch_count = 3
                    for _ in range(min(sac_count, len(battlefield_lands)) * copy_multiplier):
                        sac_name = _least_valuable_land_to_sacrifice(battlefield_lands, battlefield_sources)
                        if sac_name is None:
                            break
                        sac_card = battlefield_land_cards.get(sac_name)
                        if sac_card is not None:
                            _sacrifice_land(
                                sac_card, battlefield_lands, graveyard_lands, battlefield_permanents,
                                battlefield_permanent_cards, battlefield_creatures, turn
                            )
                    new_basics = _search_basic_lands(library, fetch_count * copy_multiplier, battlefield_sources)
                    for basic in new_basics:
                        battlefield_lands.append(basic["name"])
                        battlefield_land_cards[basic["name"]] = basic
                        basic_profile = basic["_mana_profile"]
                        if basic_profile:
                            _apply_mana_source(basic_profile, battlefield_sources, available, hand, basic["name"])
                if card["name"] == "Snapcaster Mage":
                    flashback_mode = flashback_mode or "single"
                    flashback_uses_left += copy_multiplier
                if card["name"] == "Past in Flames":
                    flashback_mode = "all"
                if card["name"] == "Underworld Breach":
                    flashback_mode = "escape"

                if capture_frames:
                    cast_events.append({
                        "name": card["name"],
                        "is_commander": is_commander_cast,
                        "copy_multiplier": copy_multiplier,
                        "x_value": x_value,
                        "tapped": [{"name": s["name"], "colors": sorted(s["colors"])} for s in tapped_sources],
                        "permanents_added": permanents_added,
                        "drew": drew,
                        "discarded": discarded,
                        "graveyard_size": len(graveyard),
                    })

                progressed = True
                break

        tribal_tokens_created_this_turn = 0
        if win_tribal_type:
            tribal_token_count, tribal_tokens_created_this_turn = _run_combat_step(
                profile, turn, commander_card, battlefield_creatures, battlefield_permanents,
                battlefield_permanent_cards, available, hand, library, cast_this_turn_names,
                tribal_token_count, tribal_trigger, rng, etb_fired_this_turn, rumor_gatherer_state
            )
            if win_turn is None and win_tribal_threshold is not None and tribal_token_count >= win_tribal_threshold:
                win_turn = turn
                win_label = f"made the {win_tribal_threshold}th {win_tribal_type}!"

        if win_turn is None and win_creature_threshold is not None:
            qualifying_creatures = [
                c for c in battlefield_creatures
                if win_creature_min_cmc is None or c["card"].get("cmc", 0) >= win_creature_min_cmc
            ]
            if len(qualifying_creatures) >= win_creature_threshold:
                win_turn = turn
                win_label = (
                    f"controls {win_creature_threshold} creatures!" if win_creature_min_cmc is None
                    else f"controls {win_creature_threshold} big creatures!"
                )

        if capture_frames:
            # Everything produced this turn either got spent (on a fixed
            # cost or dumped into an X) or is sitting unspent at the end --
            # summing both gives the total mana produced this turn.
            mana_produced_this_turn = (
                sum(len(c["tapped"]) + (c["x_value"] or 0) for c in cast_events)
                + len(available) + len(flash_mana) + treasure_count
            )
            frames.append({
                "turn": turn,
                "drew_card": drew_card,
                "lands": land_events,
                "casts": cast_events,
                "mana_available_end": len(battlefield_sources),
                "mana_produced_this_turn": mana_produced_this_turn,
                "treasures_banked_end": treasure_count,
                "lands_in_play": list(battlefield_lands),
                "permanents_in_play": list(battlefield_permanents) + ["Treasure"] * treasure_count,
                "tapped_at_end": (
                    _tapped_source_names(battlefield_sources, available)
                    + ([commander_card["name"]] if commander_tapped_for_mana and commander_card else [])
                    + activated_rock_names
                ),
                "hand_end": [c["name"] for c in hand],
                "graveyard": list(graveyard),
                "win_spell": win_spell_name if win_turn == turn else None,
                "win_x_value": win_x_value if win_turn == turn else None,
                "win_label": win_label if win_turn == turn else None,
                "tribal_tokens_created": tribal_tokens_created_this_turn if win_tribal_type else None,
                "tribal_token_count": tribal_token_count if win_tribal_type else None,
                "upkeep_hits": upkeep_hits,
            })

        if win_turn is not None:
            break  # no need to keep playing once the deck has won

    return first_cast_turn, frames, win_turn


def build_library(entries, card_data, deck_colors):
    """Expand (qty, name) pairs into a list of card dicts (one per physical
    copy), skipping any name Scryfall didn't recognize. Every card gets a
    precomputed '_mana_profile' (None if it isn't a mana source). Returns
    (library, unknown_names)."""
    land_type_lines = [info["type_line"] for info in card_data.values() if info["is_land"]]

    library = []
    unknown = []
    for qty, name in entries:
        info = card_data.get(name)
        if info is None:
            unknown.append(name)
            continue
        for _ in range(qty):
            card = dict(info)
            card["_mana_profile"] = resolve_mana_profile(info, deck_colors, land_type_lines)
            card["_draw_profile"] = resolve_draw_profile(info)
            card["_untap_lands"] = resolve_untap_lands(info)
            library.append(card)
    return library, unknown


_deck_cache = {}


def _deck_cache_key(entries, commander_name):
    return (tuple(sorted(entries)), commander_name)


def prepare_deck(entries, commander_name):
    """Fetches Scryfall data and builds the library/commander once per
    unique (entries, commander_name), caching the result -- so re-simulating
    a specific game index for the replay viewer doesn't re-hit Scryfall."""
    key = _deck_cache_key(entries, commander_name)
    if key in _deck_cache:
        return _deck_cache[key]

    all_names = [name for _, name in entries]
    if commander_name:
        all_names = all_names + [commander_name]
    card_data = fetch_card_data(all_names)

    unknown_upfront = [n for n in all_names if n not in card_data]

    deck_colors = set()
    for name in all_names:
        info = card_data.get(name)
        if info:
            deck_colors |= set(info["color_identity"])

    library_template, unknown = build_library(entries, card_data, deck_colors)
    unknown = list(dict.fromkeys(unknown_upfront + unknown))

    commander_info = None
    if commander_name and commander_name in card_data:
        commander_info = dict(card_data[commander_name])
        commander_info["_mana_profile"] = resolve_mana_profile(commander_info, deck_colors)
        commander_info["_draw_profile"] = resolve_draw_profile(commander_info)
        commander_info["_untap_lands"] = resolve_untap_lands(commander_info)
        commander_info["_firebending"] = _parse_firebending(commander_info["oracle_text"])

    bundle = {
        "card_data": card_data,
        "deck_colors": deck_colors,
        "library_template": library_template,
        "commander_info": commander_info,
        "unknown": unknown,
    }
    _deck_cache[key] = bundle
    return bundle


def replay_single_game(entries, commander_name, game_index, max_turns, run_seed, on_the_play=True, profile=None):
    """Deterministically re-simulates one specific game (by index, within a
    given run_seed) with full frame capture, for the web UI's game player.
    `run_seed` must match the value returned by the run_simulation() call
    that produced the game numbering the caller is browsing -- otherwise
    "game #N" won't refer to the same game the stats were computed from.
    Returns {"frames": [...], "win_turn": int_or_None, "game_index": game_index}."""
    bundle = prepare_deck(entries, commander_name)
    profile = profile or find_profile_for_commander(commander_name)
    deck_cards = [dict(c) for c in bundle["library_template"]]
    commander_card = dict(bundle["commander_info"]) if bundle["commander_info"] else None
    rng = random.Random(f"{run_seed}:{game_index}")
    _, frames, win_turn = simulate_game(
        deck_cards, commander_card, max_turns, rng, on_the_play, profile, capture_frames=True
    )
    return {"frames": frames, "win_turn": win_turn, "game_index": game_index}


def run_simulation(entries, commander_name, num_games, max_turns, on_the_play=True, profile=None):
    """Fetches card data, runs `num_games` solitaire games, and aggregates
    per-card cast-by-turn stats. `profile` (see DEFAULT_PROFILE) supplies
    deck-specific sequencing rules -- pass find_profile_for_commander(name)
    to auto-apply a saved one, or omit to use generic defaults. Each game is
    seeded by its index so it can be exactly replayed later via
    replay_single_game(). The fastest game to cast an X spell for
    WIN_X_THRESHOLD+ is picked as the default one to show in the game player.
    Returns a result dict for the web UI."""
    profile = profile or find_profile_for_commander(commander_name)
    bundle = prepare_deck(entries, commander_name)
    card_data = bundle["card_data"]
    deck_colors = bundle["deck_colors"]
    library_template = bundle["library_template"]
    commander_info = bundle["commander_info"]
    unknown = bundle["unknown"]

    # Fresh every call (unlike the per-game seed below) so repeated runs
    # actually see different games -- games are only reproducible *within*
    # one run, by combining this with a game index.
    run_seed = random.SystemRandom().randrange(2**31)

    nonland_names = [c["name"] for c in library_template if not c["is_land"]]
    if commander_info:
        nonland_names.append(commander_info["name"])
    tracked_names = list(dict.fromkeys(nonland_names))

    cast_turns = {name: [] for name in tracked_names}
    winning_games = []  # (game_index, win_turn)

    for game_index in range(num_games):
        deck_cards = [dict(c) for c in library_template]
        commander_card = dict(commander_info) if commander_info else None
        rng = random.Random(f"{run_seed}:{game_index}")
        first_cast, _, win_turn = simulate_game(
            deck_cards, commander_card, max_turns, rng, on_the_play, profile
        )
        for name in tracked_names:
            if name in first_cast:
                cast_turns[name].append(first_cast[name])
        if win_turn is not None:
            winning_games.append((game_index, win_turn))

    winning_games.sort(key=lambda t: t[1])
    default_game_index = winning_games[0][0] if winning_games else 0
    default_replay = replay_single_game(
        entries, commander_name, default_game_index, max_turns, run_seed, on_the_play, profile
    )

    top_games = []
    for game_index, win_turn in winning_games[:10]:
        replay = replay_single_game(entries, commander_name, game_index, max_turns, run_seed, on_the_play, profile)
        top_games.append({
            "game_index": game_index,
            "win_turn": win_turn,
            "last_frame": replay["frames"][-1],
        })

    stats = []
    for name in tracked_names:
        turns_cast = cast_turns[name]
        never_cast = num_games - len(turns_cast)
        avg_turn = sum(turns_cast) / len(turns_cast) if turns_cast else None
        by_turn = []
        for t in range(1, max_turns + 1):
            pct = 100.0 * sum(1 for x in turns_cast if x <= t) / num_games
            by_turn.append(pct)
        info = card_data.get(name) or (commander_info if commander_info and commander_info["name"] == name else {})
        stats.append({
            "name": name,
            "image_url": info.get("image_url") if info else None,
            "avg_turn": avg_turn,
            "pct_by_turn": by_turn,
            "pct_never": 100.0 * never_cast / num_games,
            "is_commander": commander_info is not None and name == commander_info["name"],
        })
    stats.sort(key=lambda s: (s["avg_turn"] is None, s["avg_turn"]))

    win_turns = [t for _, t in winning_games]
    avg_win_turn = sum(win_turns) / len(win_turns) if win_turns else None
    median_win_turn = statistics.median(win_turns) if win_turns else None
    fastest_win_turn = min(win_turns) if win_turns else None
    # (no "slowest win" stat -- with the early-exit-on-win + a fixed turn
    # cap, that number just converges to the cap itself and says nothing
    # useful; the win-turn distribution below tells the real story.)
    win_turn_distribution = [win_turns.count(t) for t in range(1, max_turns + 1)]

    commander_stat = next((s for s in stats if s["is_commander"]), None)
    avg_cards_cast_per_game = sum(len(v) for v in cast_turns.values()) / num_games

    card_images = {name: info["image_url"] for name, info in card_data.items() if info.get("image_url")}
    if commander_info and commander_info.get("image_url"):
        card_images[commander_info["name"]] = commander_info["image_url"]
    card_images["Treasure"] = TREASURE_TOKEN_IMAGE_URL

    return {
        "num_games": num_games,
        "max_turns": max_turns,
        "deck_colors": sorted(deck_colors),
        "commander_name": commander_info["name"] if commander_info else None,
        "unknown_names": unknown,
        "library_size": len(library_template),
        "stats": stats,
        "profile_file": profile.get("file"),
        "run_seed": run_seed,
        "default_game_index": default_game_index,
        "has_winning_game": bool(winning_games),
        "win_rate": 100.0 * len(winning_games) / num_games,
        "avg_win_turn": avg_win_turn,
        "median_win_turn": median_win_turn,
        "fastest_win_turn": fastest_win_turn,
        "win_turn_distribution": win_turn_distribution,
        "win_turn_distribution_max": max(win_turn_distribution) if win_turn_distribution else 0,
        "commander_avg_turn": commander_stat["avg_turn"] if commander_stat else None,
        "commander_pct_never": commander_stat["pct_never"] if commander_stat else None,
        "avg_cards_cast_per_game": avg_cards_cast_per_game,
        "replay": default_replay["frames"],
        "card_images": card_images,
        "top_games": top_games,
    }
