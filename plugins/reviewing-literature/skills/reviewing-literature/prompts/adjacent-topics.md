# Adjacent topics (step 7)

You map the papers the review screened out into the neighbouring research topics they belong to, so a
reader can see the landscape around the review's scope and where its boundary lies.

## Inputs (given in your dispatch message)

- `review`: the review folder.
- `packet`: `<review>/data/adjacent/input.json`, written by `litrev.py adjacent-input`. It holds the review's
  `purpose`, its `scope` (`in`, `out`, `protocol`), the `decisions` log, and every excluded paper under `papers`
  (id, title, year, citations, `excluded_because`, the head of the abstract), most cited first.

## Task

Read ONLY `packet`. No outside knowledge about the papers, no web, no full texts.

Put every paper in exactly one of three places:

1. **A topic**: a research area next to the scope that the paper works on (e.g. "UAV-to-satellite geo-localization",
   "Panorama-only cross-view retrieval", "Map-based localization with OpenStreetMap"). Start from the scope's `out`
   items and the scope decisions; add a topic only when several papers share an area none of those name. Aim for
   6–12 topics with at least 3 papers each; put a lone paper in the closest topic. A topic of generic building
   blocks the field reuses (backbones, feature matchers, depth estimators) is allowed when the excluded set holds
   several of them. Name topics by what the papers do, not by a paper.
2. **`in_scope`**: the paper fits the scope's `in` items but was excluded for another reason (no readable full
   text, outside the year window, over the paper cap). These are not adjacent topics; the hub lists them apart.
3. **`unrelated`**: the paper is neither in scope nor in any coherent neighbouring area (another field, a
   tutoring system, a channel model). The hub only counts these.

Judge from the title and abstract first; `excluded_because` was often written for a batch and can lump several
areas together ("off-topic by title (drone/UAV ..., VLM geolocation, OSM ...)"), so split such batches by topic.

Write ONLY `<review>/data/adjacent/topics.json`:

```
{"topics": [
  {"name": "short topic name",
   "definition": "one plain sentence: what these papers do",
   "relation": "one sentence: how it differs from the review's scope and what it shares with it",
   "scope_out": "the scope `out` item it falls under, copied verbatim, or null",
   "decision_refs": ["DEC003"],
   "paper_ids": ["P123", "P456"]}],
 "in_scope": ["P789"],
 "unrelated": ["P012"]}
```

## Rules

- Every paper in `papers` appears exactly once across `topics[].paper_ids`, `in_scope` and `unrelated`.
- `auto_in_scope` only counts the papers left out of the packet by rule (screened on-topic, excluded only by the
  cap, the year window or the seminal quota). merge-adjacent adds them to `in_scope`; do not list them.
- Order topics from the closest to the scope to the farthest.
- `decision_refs` lists only ids present in the packet's `decisions`.
- Return nothing else.
