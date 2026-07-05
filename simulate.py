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
DEFAULT_PROFILE = {
    "commander": None,
    "hold_until_commander_resolves": [],
    "commander_copies_spells_while_attacking": False,
    "reserve_mana_kinds_for_x_spells": [],
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

ARCHIDEKT_URL_RE = re.compile(r"archidekt\.com/decks/(\d+)", re.I)
_MANA_TOKEN_RE = re.compile(r"\{([^}]+)\}")
_ADD_CLAUSE_RE = re.compile(r"[Aa]dd ([^.]+)\.")
_TREASURE_RE = re.compile(r"create (a|an|one|two|three|four|five|six|\d+) treasure tokens?")
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _word_to_number(word):
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word, 1)


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
    if not mana_cost and card.get("card_faces"):
        front = card["card_faces"][0]
        mana_cost = front.get("mana_cost", "") or mana_cost
        cmc = front.get("cmc", cmc)
        oracle_text = oracle_text or front.get("oracle_text") or ""

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


def resolve_mana_profile(card_info, deck_colors):
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
    0 for lands and cost-free rocks, which just produce for free."""
    text = card_info["oracle_text"]
    if "Firebending" in text:
        return None  # attack-triggered, handled separately in simulate_game
    text_lower = text.lower()
    type_line = card_info["type_line"]
    is_land = card_info["is_land"]
    is_instant_or_sorcery = "Instant" in type_line or "Sorcery" in type_line

    is_land_search = "search your library for" in text_lower and "land card" in text_lower
    if is_land_search:
        named_colors = {color for color, basic in BASIC_LAND_BY_COLOR.items() if basic in text}
        fetch_colors = named_colors or (set(deck_colors) or {"C"})
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
    the cost and produce the mana fresh each time. Returns the updated
    `available` list (does not mutate the input)."""
    for rock in rocks:
        cost = rock["activation_cost"]
        if cost <= len(available):
            available = available[cost:] + [
                {"name": rock["source_name"], "colors": frozenset(rock["produces"])}
                for _ in range(rock["amount"])
            ]
    return available


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
    makers, which increase what's available) > flashback/escape enablers and
    copy-multiplier engine pieces (Veyran, Twinning Staff -- cast main-phase
    so graveyard rituals are recastable and the copy trigger is upgraded
    before we commit to an X spell) > X spells (dump everything into
    maximizing X, since our no-opponent model doesn't value anything "other
    spells" do anyway) > everything else (only gets whatever's left, which
    after an X spell dumps its mana is usually nothing this turn)."""
    if card is commander_card:
        return 0
    if card.get("_mana_profile"):
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
# X spells that only ever hit creatures, not players -- removal, not a
# finisher, so a big X on these shouldn't count as "winning" the game.
WIN_EXCLUDED_CARDS = {"Shellshock", "Street Spasm"}


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
    WIN_X_THRESHOLD+ (or None if it never happened)."""
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
    commander_cast_turn = None
    firebending_amount = (commander_card or {}).get("_firebending", 0)
    treasure_count = 0  # banked across turns
    treasure_colors = set()

    for turn in range(1, max_turns + 1):
        drew_card = None
        land_event = None
        cast_events = []

        if not (turn == 1 and on_the_play) and library:
            drawn = library.pop(0)
            hand.append(drawn)
            drew_card = drawn["name"]

        hand_lands = [c for c in hand if c["is_land"]]
        if hand_lands:
            land = _choose_land_to_play(hand_lands, battlefield_sources)
            hand.remove(land)
            battlefield_lands.append(land["name"])
            mana_profile = land["_mana_profile"]
            fetched_color = None
            if mana_profile:
                fetched_color = _apply_mana_source(mana_profile, battlefield_sources, [], hand, land["name"])
            land_event = {"name": land["name"], "fetched": fetched_color}

        available = list(battlefield_sources)
        available = _activate_costed_rocks(battlefield_costed_rocks, available)
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
                    x_value = len(available) + len(flash_mana) + treasure_count
                    available = []
                    flash_mana = []
                    treasure_count = 0
                    if win_turn is None and x_value >= WIN_X_THRESHOLD and card["name"] not in WIN_EXCLUDED_CARDS:
                        win_turn = turn
                        win_spell_name = card["name"]
                        win_x_value = x_value

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
                if is_commander_cast or _is_permanent_type(card["type_line"]):
                    battlefield_permanents.append(card["name"])
                    battlefield_permanent_cards.append(card)
                    permanents_added = 1
                    # The legend rule: extra copies of a Legendary permanent
                    # get sacrificed immediately, so only one ever sticks
                    # around no matter how many times it was copied.
                    if "Legendary" not in card["type_line"]:
                        for _ in range(extra_copies):
                            battlefield_permanents.append(card["name"] + " (copy)")
                            battlefield_permanent_cards.append(card)
                            permanents_added += 1
                elif not from_graveyard:
                    graveyard.append(card["name"])  # Instants/Sorceries go to the graveyard once resolved
                    graveyard_cards.append(card)

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
                            available = _activate_costed_rocks([rock], available)
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
                    })

                progressed = True
                break

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
                "land": land_event,
                "casts": cast_events,
                "mana_available_end": len(battlefield_sources),
                "mana_produced_this_turn": mana_produced_this_turn,
                "treasures_banked_end": treasure_count,
                "lands_in_play": list(battlefield_lands),
                "permanents_in_play": list(battlefield_permanents) + ["Treasure"] * treasure_count,
                "tapped_at_end": (
                    _tapped_source_names(battlefield_sources, available)
                    + ([commander_card["name"]] if commander_tapped_for_mana and commander_card else [])
                ),
                "hand_end": [c["name"] for c in hand],
                "graveyard": list(graveyard),
                "win_spell": win_spell_name if win_turn == turn else None,
                "win_x_value": win_x_value if win_turn == turn else None,
            })

        if win_turn is not None:
            break  # no need to keep playing once the deck has won

    return first_cast_turn, frames, win_turn


def build_library(entries, card_data, deck_colors):
    """Expand (qty, name) pairs into a list of card dicts (one per physical
    copy), skipping any name Scryfall didn't recognize. Every card gets a
    precomputed '_mana_profile' (None if it isn't a mana source). Returns
    (library, unknown_names)."""
    library = []
    unknown = []
    for qty, name in entries:
        info = card_data.get(name)
        if info is None:
            unknown.append(name)
            continue
        for _ in range(qty):
            card = dict(info)
            card["_mana_profile"] = resolve_mana_profile(info, deck_colors)
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
