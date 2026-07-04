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
        resp = requests.post(
            f"{SCRYFALL_ROOT}/cards/collection",
            headers=HEADERS,
            json={"identifiers": [{"name": n} for n in batch]},
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

    return {
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


# ---------------------------------------------------------------------------
# Mana modeling
# ---------------------------------------------------------------------------

def resolve_mana_profile(card_info, deck_colors):
    """Figures out what a card (land, mana rock, ritual spell, mana dork --
    anything) contributes to the mana pool. Returns None if it isn't a mana
    source at all. Otherwise:
        {"kind": "permanent" | "ritual", "is_fetch": bool,
         "fetch_colors": set(colors), "produces": set(colors), "amount": int}
    "permanent" sources (lands, rocks, dorks) keep producing every future
    turn; "ritual" sources (Dark Ritual, Seething Song, ...) only add mana
    for the turn they're cast."""
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
                "produces": set(), "amount": 1}

    if "any color" in text_lower:
        treasure_match = _TREASURE_RE.search(text_lower)
        if treasure_match:
            # Unlike a ritual's burst, Treasures are actual permanents that
            # stick around until sacrificed -- banked separately so they
            # carry over to later turns instead of evaporating unused.
            return {"kind": "treasure", "is_fetch": False, "fetch_colors": set(),
                    "produces": set(deck_colors) or {"C"}, "amount": _word_to_number(treasure_match.group(1))}
        return {"kind": "ritual" if is_instant_or_sorcery else "permanent",
                "is_fetch": False, "fetch_colors": set(),
                "produces": set(deck_colors) or {"C"}, "amount": 1}

    if not card_info.get("produced_mana"):
        return None

    add_clauses = _ADD_CLAUSE_RE.findall(text)
    if not add_clauses:
        return None

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

    kind = "permanent" if is_land or not is_instant_or_sorcery else "ritual"
    return {"kind": kind, "is_fetch": False, "fetch_colors": set(), "produces": produces, "amount": amount}


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
    """available_sources: list of frozenset(colors), one per untapped mana
    source. Greedy matcher (not a true optimal solver): satisfies the most
    color-constrained pips first, preferring the least-flexible matching
    source so duals are saved for later pips. Returns the list of source
    indices that would be spent, or None if it can't be paid."""
    indexed = list(enumerate(available_sources))
    used = set()
    for pip in sorted(colored_pips, key=len):
        candidates = [i for i, colors in indexed if i not in used and colors & pip]
        if not candidates:
            return None
        best = min(candidates, key=lambda i: len(indexed[i][1]))
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


def _choose_discard(hand, battlefield_sources):
    """Discards the least useful card: a land that would add the fewest
    new colors if there's a spare one, otherwise the priciest spell."""
    lands_in_hand = [c for c in hand if c["is_land"]]
    if lands_in_hand:
        have = {color for source in battlefield_sources for color in source}

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


def _draw_opening_hand(library):
    hand = library[:7]
    for _ in range(MAX_MULLIGAN_ATTEMPTS):
        random.shuffle(library)
        hand = library[:7]
        lands = sum(1 for c in hand if c["is_land"])
        if MULLIGAN_MIN_LANDS <= lands <= MULLIGAN_MAX_LANDS:
            return hand, library[7:]
    return hand, library[7:]  # give up, keep the last hand drawn


def _choose_fetch_color(fetch_colors, current_sources, hand):
    if not fetch_colors:
        return None
    have = {c for src in current_sources for c in src}
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
    have = {c for src in current_sources for c in src}

    def new_color_count(land):
        profile = land["_mana_profile"]
        if not profile:
            return -1  # a land with no mana ability at all -- last resort
        pool = profile["fetch_colors"] if profile["is_fetch"] else profile["produces"]
        return len(pool - have)

    return max(hand_lands, key=new_color_count)


def _apply_mana_source(profile, battlefield_sources, available, hand):
    """Adds the mana a just-played/cast source provides to `available`
    (this turn) and, if it's a permanent source, to `battlefield_sources`
    (every future turn too). Mutates both lists in place."""
    if profile["is_fetch"]:
        color = _choose_fetch_color(profile["fetch_colors"], battlefield_sources, hand)
        if color is None:
            return
        source = frozenset({color})
        battlefield_sources.append(source)
        available.append(source)
        return

    if not profile["produces"]:
        return
    source = frozenset(profile["produces"])
    new_sources = [source] * profile["amount"]
    if profile["kind"] == "permanent":
        battlefield_sources.extend(new_sources)
    available.extend(new_sources)


def _is_instant_or_flash(card):
    return "Instant" in card["type_line"] or "flash" in card["oracle_text"].lower()


def _has_x_cost(mana_cost):
    return "{X}" in (mana_cost or "")


def _is_permanent_type(type_line):
    return not ("Instant" in type_line or "Sorcery" in type_line)


def _cast_priority(card, commander_card):
    """Lower sorts first: commander > mana sources (rituals/rocks/treasure
    makers) > everything else > X spells (held back to mop up whatever
    mana is left over after everything else is cast)."""
    if card is commander_card:
        return 0
    if _has_x_cost(card["mana_cost"]):
        return 3
    if card.get("_mana_profile"):
        return 1
    return 2


_FIREBENDING_RE = re.compile(r"Firebending (\d+)")


def _parse_firebending(oracle_text):
    """Firebending N: 'Whenever this creature attacks, add N red mana.'
    Returns N, or 0 if the card doesn't have the keyword."""
    match = _FIREBENDING_RE.search(oracle_text)
    return int(match.group(1)) if match else 0


def _matches_hold_tag(card, tag):
    tag_lower = tag.lower()
    return tag_lower in card["type_line"].lower() or tag_lower in card["oracle_text"].lower()


def simulate_game(deck_cards, commander_card, max_turns, on_the_play=True, profile=None):
    """Plays one solitaire game. `deck_cards` is the 99/100-card library
    (each a dict from fetch_card_data, annotated with a precomputed
    '_mana_profile'). `commander_card`, if given, is cast from an
    always-available command zone alongside the hand and given top casting
    priority every turn. `profile` (see DEFAULT_PROFILE) supplies deck-
    specific sequencing rules; falls back to generic defaults if omitted.
    If the commander has a "Firebending N" ability, we assume it attacks
    every turn starting the turn after it's cast, adding N red mana usable
    only on Instant/Flash spells that turn (matching the ability's real
    "lasts until end of combat" wording). Returns
    ({card_name: first_turn_cast}, turn_by_turn_log_text)."""
    profile = profile or DEFAULT_PROFILE
    hold_tags = profile.get("hold_until_commander_resolves", [])
    copies_while_attacking = profile.get("commander_copies_spells_while_attacking", False)
    reserve_kinds = set(profile.get("reserve_mana_kinds_for_x_spells", []))

    library = list(deck_cards)
    hand, library = _draw_opening_hand(library)

    battlefield_sources = []  # list of frozenset(colors), one per permanent mana source
    battlefield_lands = []  # names of lands in play
    battlefield_permanents = []  # names of nonland permanents (creatures/artifacts/etc.) in play
    first_cast_turn = {}
    log = []
    commander_cast_turn = None
    firebending_amount = (commander_card or {}).get("_firebending", 0)
    treasure_count = 0  # banked across turns
    treasure_colors = set()

    for turn in range(1, max_turns + 1):
        turn_log = [f"Turn {turn}:"]

        if not (turn == 1 and on_the_play) and library:
            drawn = library.pop(0)
            hand.append(drawn)
            turn_log.append(f"  Draw: {drawn['name']}")

        hand_lands = [c for c in hand if c["is_land"]]
        if hand_lands:
            land = _choose_land_to_play(hand_lands, battlefield_sources)
            hand.remove(land)
            battlefield_lands.append(land["name"])
            mana_profile = land["_mana_profile"]
            note = ""
            if mana_profile:
                before = len(battlefield_sources)
                _apply_mana_source(mana_profile, battlefield_sources, [], hand)
                if mana_profile["is_fetch"]:
                    note = f" (fetched {sorted(battlefield_sources[-1])[0] if len(battlefield_sources) > before else 'nothing'})"
            turn_log.append(f"  Land: {land['name']}{note}")
        else:
            turn_log.append("  Land: (none in hand)")

        available = list(battlefield_sources)
        is_attacking = commander_cast_turn is not None and turn > commander_cast_turn
        flash_mana = [frozenset({"R"})] * firebending_amount if is_attacking else []
        cast_this_turn = []

        # Hold whatever this deck's profile says to hold until the
        # commander has resolved (e.g. Instants/Flash worth more once it's
        # out attacking and copying them).
        commander_is_out = commander_cast_turn is not None
        pool = [
            c for c in hand
            if not c["is_land"]
            and (commander_is_out or not any(_matches_hold_tag(c, tag) for tag in hold_tags))
        ]
        if commander_card is not None and commander_cast_turn is None:
            pool.append(commander_card)

        progressed = True
        while progressed:
            progressed = False
            candidates = sorted(
                pool,
                key=lambda c: (_cast_priority(c, commander_card), -c["cmc"]),
            )
            for card in candidates:
                generic, pips = parse_mana_cost(card["mana_cost"])
                has_x = _has_x_cost(card["mana_cost"])
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
                    combined += [frozenset(treasure_colors or {"C"})] * treasure_count

                used = try_pay(combined, generic, pips)
                if used is None:
                    continue

                n_avail = len(available)
                n_flash = len(flash_mana) if flash_eligible else 0
                avail_used = {i for i in used if i < n_avail}
                flash_used = {i - n_avail for i in used if n_avail <= i < n_avail + n_flash}
                treasures_used = sum(1 for i in used if i >= n_avail + n_flash)

                available = [c for i, c in enumerate(available) if i not in avail_used]
                if flash_eligible:
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

                is_commander_cast = commander_card is not None and card is commander_card
                if is_commander_cast:
                    commander_cast_turn = turn
                else:
                    hand.remove(card)
                pool.remove(card)

                if card["name"] not in first_cast_turn:
                    first_cast_turn[card["name"]] = turn
                label = card["name"]
                if is_commander_cast:
                    label += " (commander)"
                elif x_value is not None:
                    label += f" (X={x_value})"
                cast_this_turn.append(label)

                is_copied = is_attacking and copies_while_attacking and not is_commander_cast

                if is_commander_cast or _is_permanent_type(card["type_line"]):
                    battlefield_permanents.append(card["name"])
                    if is_copied:
                        battlefield_permanents.append(card["name"] + " (copy)")

                mana_profile = card.get("_mana_profile")
                if mana_profile:
                    if mana_profile["kind"] == "treasure":
                        treasure_count += mana_profile["amount"] * (2 if is_copied else 1)
                        treasure_colors = mana_profile["produces"]
                    else:
                        _apply_mana_source(mana_profile, battlefield_sources, available, hand)
                        if is_copied:
                            _apply_mana_source(mana_profile, battlefield_sources, available, hand)  # copied by the attack trigger

                draw_profile = card.get("_draw_profile")
                if draw_profile:
                    if draw_profile["discard"]:
                        discarded = _discard_cards(hand, draw_profile["discard"], battlefield_sources)
                        pool = [c for c in pool if not any(c is d for d in discarded)]
                    copies_to_resolve = 2 if is_copied else 1
                    for _ in range(draw_profile["draw"] * copies_to_resolve):
                        if library:
                            hand.append(library.pop(0))
                    if draw_profile["draw"]:
                        drawn_note = f" [drew {draw_profile['draw'] * copies_to_resolve}"
                        if draw_profile["discard"]:
                            drawn_note += f", discarded {draw_profile['discard']}"
                        drawn_note += " (copied, no extra discard)]" if is_copied else "]"
                        label += drawn_note
                        cast_this_turn[-1] = label

                progressed = True
                break

        turn_log.append(f"  Mana available: {len(battlefield_sources)}")
        if treasure_count:
            turn_log.append(f"  Treasures banked: {treasure_count}")
        turn_log.append(f"  Cast: {', '.join(cast_this_turn) if cast_this_turn else '(nothing)'}")
        turn_log.append(f"  Lands: {', '.join(battlefield_lands) if battlefield_lands else '(none)'}")
        turn_log.append(f"  Permanents: {', '.join(battlefield_permanents) if battlefield_permanents else '(none)'}")
        turn_log.append(f"  Hand: {', '.join(c['name'] for c in hand) if hand else '(empty)'}")
        log.append("\n".join(turn_log))

    return first_cast_turn, "\n".join(log)


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
            library.append(card)
    return library, unknown


def run_simulation(entries, commander_name, num_games, max_turns, on_the_play=True, profile=None):
    """Fetches card data, runs `num_games` solitaire games, and aggregates
    per-card cast-by-turn stats. `profile` (see DEFAULT_PROFILE) supplies
    deck-specific sequencing rules -- pass find_profile_for_commander(name)
    to auto-apply a saved one, or omit to use generic defaults. Returns a
    result dict for the web UI."""
    profile = profile or find_profile_for_commander(commander_name)
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
        commander_info["_firebending"] = _parse_firebending(commander_info["oracle_text"])

    nonland_names = [c["name"] for c in library_template if not c["is_land"]]
    if commander_info:
        nonland_names.append(commander_info["name"])
    tracked_names = list(dict.fromkeys(nonland_names))

    cast_turns = {name: [] for name in tracked_names}
    sample_log = None

    for game_index in range(num_games):
        deck_cards = [dict(c) for c in library_template]
        commander_card = dict(commander_info) if commander_info else None
        first_cast, log_text = simulate_game(
            deck_cards, commander_card, max_turns, on_the_play, profile
        )
        for name in tracked_names:
            if name in first_cast:
                cast_turns[name].append(first_cast[name])
        if game_index == 0:
            sample_log = log_text

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

    return {
        "num_games": num_games,
        "max_turns": max_turns,
        "deck_colors": sorted(deck_colors),
        "commander_name": commander_info["name"] if commander_info else None,
        "unknown_names": unknown,
        "library_size": len(library_template),
        "stats": stats,
        "sample_log": sample_log,
        "profile_file": profile.get("file"),
    }
