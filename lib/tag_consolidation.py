"""Pure logic for tag-vocabulary consolidation (no Nextcloud imports — fully unit-testable).

The pipeline: filter the tag vocabulary -> chunk it alphabetically -> build a dedup-framed LLM
prompt per chunk -> sanitize the model's proposed merges -> resolve chains/cycles into a flat
source->canonical mapping. `apply_aliases` is the runtime choke point that canonicalizes freshly
captioned tags.

Prompt design note: a naive "condense these tags" prompt makes the model CATEGORIZE
(bedroom->location, sunset->time), destroying search detail. The dedup framing below with explicit
wrong-examples was validated live against the production model (20/200 sensible merges, zero
categorization).
"""

from __future__ import annotations

CHUNK_SIZE = 500            # tags per LLM call; alphabetical sort keeps variants in the same chunk
CANONICAL_CARRY_CAP = 1500  # established canonicals carried into later chunks (prompt budget)
MARKER_PREFIX = "tagged by recognize"  # old recognize app's marker tag(s) — never touched


def _excluded_name(name: str) -> bool:
    """Tags no consolidation step may ever touch, on either side of a merge."""
    n = name.strip()
    return not n or ":" in n or n.lower().startswith(MARKER_PREFIX)


def filter_vocabulary(
    tags: list[tuple[int, str, bool, bool]], include_uppercase: bool
) -> list[tuple[int, str]]:
    """Select consolidation candidates from (tag_id, name, user_visible, user_assignable) rows.

    Drops person:/marker/invisible/restricted tags always; drops Capitalized names (the old
    recognize app's classifier vocabulary) unless include_uppercase.
    """
    out: list[tuple[int, str]] = []
    for tag_id, name, visible, assignable in tags:
        if not visible or not assignable:
            continue
        if _excluded_name(name):
            continue
        if not include_uppercase and name[:1].isupper():
            continue
        out.append((tag_id, name.strip()))
    return out


def chunk_vocabulary(
    vocab: list[tuple[str, int]], size: int = CHUNK_SIZE
) -> list[list[tuple[str, int]]]:
    """Case-insensitively sorted chunks so plural/spelling variants land in the same chunk."""
    ordered = sorted(vocab, key=lambda t: t[0].casefold())
    return [ordered[i : i + size] for i in range(0, len(ordered), size)] if ordered else []


def build_chunk_prompt(chunk: list[tuple[str, int]], canonicals_so_far: list[str]) -> str:
    tag_lines = "\n".join(f"{name} — {count}" for name, count in chunk)
    established = ""
    if canonicals_so_far:
        carried = ", ".join(canonicals_so_far[:CANONICAL_CARRY_CAP])
        established = (
            "\nEstablished canonical tags from earlier batches — when a variant of one of these"
            f" appears below, map it into the established tag:\n{carried}\n"
        )
    return f"""You are DEDUPLICATING the tag vocabulary of a personal photo library. Your job is to find
near-duplicate tags — NOT to categorize tags into broader groups.

A merge "from -> to" means: the tag "from" is deleted and its photos get "to" instead. A merge is
ONLY correct when a person searching for either word would be happy to get the exact same photos.
Both words must mean essentially THE SAME THING.

STRICT RULES:
- Merge: singular/plural pairs (trees -> tree), spelling or wording variants (gray/grey,
  rock-climbing/rock climbing), and words that are true interchangeable synonyms in photo search.
- "to" should be a tag from this batch or the established list — or the natural singular of "from"
  when the singular is simply missing (leaves -> leaf is fine).
- Prefer the higher-usage form as "to"; prefer singular over plural when counts are close.
- DO NOT merge a specific thing into a broader category. WRONG: bedroom -> location,
  sunset -> time, man -> person, almonds -> food, climbing -> sport. These DESTROY search detail.
- DO NOT merge related-but-different concepts: bouldering and climbing are different; sunny and
  daylight are different. Keep both.
- NEVER merge place names, proper nouns, landmarks, or brands. Leave them untouched.
- MOST TAGS SHOULD NOT BE MERGED. A healthy result merges maybe 5-15% of a batch. If in doubt,
  leave the tag alone.

GOOD examples: trees -> tree; ropes -> rope; indoors -> indoor; woods -> forest
WRONG examples (never do this): kitchen -> location; jumping -> activity; red -> color
{established}
Vocabulary batch (tag — number of photos):
{tag_lines}

Respond with ONLY JSON: {{"merges": [{{"from": "...", "to": "...", "why": "..."}}]}}
("why": 2-5 words). If nothing in this batch should be merged, respond {{"merges": []}}."""


def parse_merge_response(data: dict) -> list[tuple[str, str, str]]:
    """Extract (source, canonical, reason) triples, tolerating key-name drift."""
    out: list[tuple[str, str, str]] = []
    for item in data.get("merges") or []:
        if not isinstance(item, dict):
            continue
        src = item.get("from") or item.get("source") or ""
        dst = item.get("to") or item.get("canonical") or ""
        why = item.get("why") or item.get("reason") or ""
        if isinstance(src, str) and isinstance(dst, str) and src.strip() and dst.strip():
            out.append((src.strip(), dst.strip(), str(why).strip()))
    return out


def _safe_variant_fold(source: str, canonical: str) -> bool:
    """May `canonical` be created even though it isn't in the vocabulary? Only for mechanical
    variants of `source` (missing singular, hyphen/space or case twin) — never new concepts."""
    s, c = source.casefold(), canonical.casefold()
    if s == c:
        return True  # pure case variant
    if s.replace("-", " ") == c.replace("-", " "):
        return True  # hyphen/space variant
    for plural, singular in ((s, c), (c, s)):
        if plural == singular + "s" or plural == singular + "es":
            return True
        if plural.endswith("ies") and singular == plural[:-3] + "y":
            return True
        if plural.endswith("ves") and singular in (plural[:-3] + "f", plural[:-3] + "fe"):
            return True  # leaves -> leaf, knives -> knife
    return False


def sanitize_pairs(
    pairs: list[tuple[str, str, str]], vocab_names: set[str], include_uppercase: bool
) -> list[tuple[str, str, str]]:
    """Drop unusable model output: self-maps, hallucinated sources, excluded names, and
    canonicals that neither exist nor are mechanical variants of their source."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for source, canonical, reason in pairs:
        if source == canonical or source in seen:
            continue
        if _excluded_name(source) or _excluded_name(canonical):
            continue
        if source not in vocab_names:
            continue  # hallucinated or out-of-batch source — unverifiable, drop
        if not include_uppercase and canonical[:1].isupper():
            continue
        if canonical not in vocab_names and not _safe_variant_fold(source, canonical):
            continue  # inventing a genuinely new concept — not allowed at analyze time
        seen.add(source)
        out.append((source, canonical, reason))
    return out


def _chase(mapping: dict[str, str], start: str) -> str:
    """Follow mapping until a terminal name; the caller guarantees acyclicity."""
    cur = start
    for _ in range(len(mapping) + 1):
        if cur not in mapping:
            return cur
        cur = mapping[cur]
    return cur  # unreachable when acyclic; defensive


def resolve_mapping(
    pairs: list[tuple[str, str, str]], counts: dict[str, int]
) -> dict[str, str]:
    """Flatten proposed pairs into an acyclic source->canonical map.

    Chains a->b->c collapse to a->c, b->c. Cycles keep the highest-usage member (tie: shorter
    name, then lexicographic) as canonical. Postcondition: no key is also a value.
    """
    m = {source: canonical for source, canonical, _ in pairs}

    # Phase 1 — break cycles by deleting the winner's outgoing edge.
    for src in list(m):
        if src not in m:
            continue
        path = [src]
        cur = m[src]
        while cur in m and cur not in path:
            path.append(cur)
            cur = m[cur]
        if cur in path:  # cycle detected
            cycle = path[path.index(cur):]
            winner = sorted(cycle, key=lambda n: (-counts.get(n, 0), len(n), n))[0]
            m.pop(winner, None)

    # Phase 2 — compress chains on the now-acyclic map.
    resolved = {src: _chase(m, src) for src in m}
    resolved = {s: c for s, c in resolved.items() if s != c}
    assert not (set(resolved) & set(resolved.values())), "mapping must be flat"
    return resolved


def compose_aliases(existing: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """Fold a newly approved mapping into the persisted alias map.

    existing x->a composed with new a->b yields x->b. New edges are added one at a time and any
    edge that would close a cycle (e.g. a later run proposing tree->trees after trees->tree was
    already established) is skipped — established aliases win. Result is flat: no key is a value.
    """
    merged = dict(existing)
    for src, dst in new.items():
        if src == dst:
            continue
        cur, hops = dst, 0
        while cur in merged and cur != src and hops <= len(merged):
            cur = merged[cur]
            hops += 1
        if cur == src:
            continue  # would reverse an established chain
        merged[src] = dst
    flat = {src: _chase(merged, src) for src in merged}
    flat = {s: c for s, c in flat.items() if s != c}
    assert not (set(flat) & set(flat.values())), "alias map must be flat"
    return flat


def apply_aliases(tags: list[str], aliases: dict[str, str]) -> list[str]:
    """Canonicalize freshly captioned tags. Unknown tags pass through untouched (the vocabulary
    may grow — a later consolidation run folds them); person:/excluded names are never mapped."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        mapped = tag if _excluded_name(tag) else aliases.get(tag, tag)
        if mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out
