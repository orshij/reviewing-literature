# Method clusters (step 6)

You group the review's top-ranked papers by the method they use.

## Inputs (given in your dispatch message)

- `review`: the review folder.
- `packet`: `<review>/data/clusters/input.json`, written by `litrev.py cluster-input`. It holds the KPIs,
  the ranked papers (title, year, position, best rank, and their method notes with verbatim quotes),
  the papers with no KPI result, and the `method_families` the surveys named in step 0. The ranked
  papers are under the key `papers`; the papers listed under `unranked` are not clustered.

## Task

First read `ste-style.md` in this prompt's folder: `name`, `definition` and `distinguishing_trait` follow it. Then
read ONLY `packet`. No outside knowledge, no web, no full texts.

Group the papers by method: what is trained, from which prior or initialisation, with what
supervision, and with which architecture. Use a survey method family when one fits and name it in
`survey_family` (`"survey:P00x / <family name>"`); create a new family only when none fits. Aim for
2–5 clusters. A single-paper cluster is allowed when its method is genuinely different from every
other paper's. Every ranked paper goes in exactly one cluster. Do not pick a best paper: the script
derives each cluster's winner from the KPI ranking.

Write ONLY `<review>/data/clusters/clusters.json`:

```
{"clusters": [
  {"name": "6–12 words: what the methods do, from input to output, and how they differ",
   "definition": {"value": "one plain sentence", "rationale": "which notes of which papers support it"},
   "distinguishing_trait": "one phrase that separates it from the other clusters",
   "survey_family": "survey:P00x / <family>" or null,
   "members": [{"paper_id": "P00x",
                "field": "data|representation|loss|topology|training_scheme|contribution",
                "rationale": "why this paper belongs, one sentence",
                "quote": "copied verbatim from that paper's note in the packet",
                "location": "the note's location"}]}]}
```

## Rules

- A quote must be copied character for character from the packet; `merge-clusters` drops any quote
  that is not in the paper's cached text.
- Name clusters by method, not by paper, so that a reader outside the subfield understands the name alone: 6–12
  words that say what the methods do (from the input to the output) and what separates them from the other
  clusters. A 2–4 word label is too short; a full sentence is too long. No paper names, no acronyms the reader
  cannot know. `merge-clusters` lists a name outside 6–12 words under `style`. Examples:
  `Teacher pseudo-label distillation` → `Student models trained on depth labels from a large teacher model`;
  `Direct cross-view transformer pose regression` → `A transformer that reads both images and outputs the camera pose directly`;
  `Diffusion prior fine-tuning` → `Image generators fine-tuned to output depth from one image`.
- Return nothing else.

## Rename only

When the dispatch message says `rename only`, the clusters stay as they are: read the review's existing
`data/clusters/clusters.json` (it is your input instead of `packet`, together with `ste-style.md`) and write it back
with only `name`, `definition.value` and `distinguishing_trait` rewritten by the rules above. Keep every cluster,
member, quote, rationale and location, and keep the order. `merge-clusters` then keeps each cluster's id, winner and
explainer, because the members did not change. Return nothing else.
