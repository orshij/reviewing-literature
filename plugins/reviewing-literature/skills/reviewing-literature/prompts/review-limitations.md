# Limitations and problems of the current approaches (step 6)

You write the limitations section of the review's synthesis slide: the problems that the current method families
share, as a matrix of problems against families, and a short list of open problems that no reviewed approach solves.

## Inputs (given in your dispatch message)

- `review`: the review folder; `litrev`: the path to `litrev.py`.
- `packet`: `<review>/data/explainers/limitations.input.json`, written by `litrev.py limitations-input`. It holds the
  review's purpose, scope and KPIs, the critique `kinds`, and every method family with:
  - `members`: each paper (the `winner` first) with its method notes (each with a verbatim quote), KPI ranks,
    `availability` (code and checkpoint status from `enrich`) and `declared_limitations`: the limitations its authors
    state, each with a verbatim quote (or a status such as `not-reported` when the paper states none).
  - `winner_weak_points`: the weak points that the winner critique found (the reviewer's reading, not the
    authors'), each with a `kind`, a `rationale` and the quote it rests on (or null for an absence).
  - `missing_member_limits`: members whose stated limitations were never extracted. If this list is not empty, say so
    in your reply line; still write the file.

## Task

First read `ste-style.md` in this prompt's folder: every `title`, `text`, `detail` and `rationale` you write follows
it. Then read ONLY `packet`. No web and no full texts.

### Problems (4–8 rows)

Find the problems that two or more families share: a stated limitation that recurs, a weak point that several winners
have, an assumption that several families need, an evaluation gap that several share (all tested on one benchmark,
no spread across training seeds, no cost). Rank them by how much they matter for the review's purpose. A
problem of one family only belongs on that family's slide, not here: `merge-limitations` drops a row with fewer than
two affected families.

For each problem, fill one cell for every family in the packet:

- `affected` + `author-reported`: a member of that family states this limitation. Copy its `quote` from that member's
  `declared_limitations` or method notes in the packet, with its `paper_id` and `location`.
- `affected` + `inferred`: the winner critique found it (a `winner_weak_points` item), or the method notes show it (an
  assumption the family needs, a test it never ran). Give a `rationale` that names the weak point or note it rests on.
  Copy the quote it rests on when there is one; an absence ("no runtime is reported") has `quote: null`.
- `addresses`: a member's method note shows that the family solves or avoids the problem. Copy that note's quote, with
  its `paper_id`. This is always on the authors' words.
- `not-shown`: the packet has no evidence either way. Never guess.

**The row text must be true for every cell.** "None of the winners ..." holds only when no cell says otherwise. When a
cell's rationale names a partial case, the text names it too: "Only CURE-Med reports a spread, over three runs with one
fixed seed; no winner reports a spread across training seeds." A spread over repeated runs (the same seed, or repeated
evaluation) and a spread across independent training seeds measure different things: say which one a paper reports.
An outside reader checked "no winner reports run-to-run spread" against CURE-Med's mean ± std over three runs and
called it wrong (llm-to-domain-slm, 2026-10-26).

### Open problems (2–5 items)

Name the open problems of the field that no reviewed family solves and that matter for the review's purpose. Here,
and only here, you may use your general knowledge of the field. The hub labels these items "From general knowledge of
the field, not from the reviewed papers". Rules: name no paper, dataset, method or number that is not in the packet
(nobody can check it); never claim that a reviewed family has or lacks something that the packet does not show; link
each item to the problem rows it extends in `related` when there are any.

Write ONLY `<review>/data/explainers/limitations.json`:

```
{"problems": [
  {"id": "L1", "title": "≤10 words: the problem", "kind": "evaluation|comparison|generalisation|assumption|cost|reproducibility|data|claim",
   "text": "one to three short sentences: the problem, which families it affects and why it matters for the purpose",
   "detail": "40–80 words: the papers, tables, settings and numbers behind it",
   "emphasis": ["key phrase from text"],
   "cells": {"C001": {"state": "affected", "basis": "author-reported", "paper_id": "P00x",
                      "quote": "copied verbatim from the packet", "location": "Sec 5"},
             "C002": {"state": "affected", "basis": "inferred", "paper_id": "P00y",
                      "quote": null, "rationale": "the winner critique: no spread across runs in Tables 1-3"},
             "C003": {"state": "not-shown"}}}],
 "open_problems": [
  {"title": "≤10 words", "text": "one or two short sentences", "emphasis": ["key phrase from text"], "related": ["L1"]}]}
```

## Rules

- Every `quote` is copied character for character from the packet, which copied it from the paper's cached text.
  `merge-limitations` checks it against that text: a cell on the authors' words whose quote fails becomes
  `not-shown`; an inferred cell keeps its point and loses the quote.
- A cell's `paper_id` must be a member of that family.
- Name each family in `text` and `detail` by its C-id next to a short form of its cluster `name` ("C002 (top-down
  search)"); the hub turns it into the family's colour dot and link.
- `emphasis`: 1 phrase per text, 2 when the `text` has 40 words or more, each copied character for character (same
  case) from its own `text`, at most 6 words: the words that carry the problem. Never a bare number or name.
- KPI numbers must be the packet's numbers. Claims about code or weights come only from `availability`: `unknown`
  means the review found no release, never that none exists.
- Weak points are the reviewer's reading: a row built only on them says so in its `text` ("the reviewer finds").
- Plain words in the style of `ste-style.md`; define any technical term in the sentence that uses it.
- Before you finish, run `python3 <litrev> check-json <your file>` and fix any error it reports.
- Return one line: the number of problems and open problems, and any `missing_member_limits`.
