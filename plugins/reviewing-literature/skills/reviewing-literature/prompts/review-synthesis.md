# Review synthesis (step 6)

You write the closing slide of a literature review: what its method families add up to, as an answer
to the review's own question.

## Inputs (given in your dispatch message)

- `review`: the review folder.
- `packet`: `<review>/data/explainers/synthesis.input.json`, written by `litrev.py synthesis-input`. It holds
  the review's purpose (its question), the KPIs, every method cluster with its members' method notes
  (each with a verbatim quote), each member's KPI ranks and `availability` (code and checkpoint status from
  `enrich`), the explainer's headline and facets, the papers no KPI could rank, and `counts`: the paper counts
  computed from the data (families, family members, ranked papers, unranked papers with no board result, papers on
  each KPI board, and members with code, a checkpoint or both, in total and per family).

## Task

First read `ste-style.md` in this prompt's folder: every `text` and `detail` you write follows it. Then read ONLY
`packet`. No outside knowledge, no web, no full texts.

Write 3–5 takeaways that answer the purpose directly: which families exist, how they differ in data,
supervision, prior and output, how they compare on the KPIs, and what is available to use (only if
the packet says so). Each takeaway's `text` is two to four short sentences that compare families and carry only the
numbers the finding needs; a takeaway about a single paper belongs on that paper's slide, not here. Its `detail`
(40–80 words, shown in a "More detail" fold, collapsed by default) gives the numbers, ranks and paper names behind
the finding. Say where the evidence is thin (few
papers on a board, families with no ranked member). Limitations, weak points and gaps that several families share
are not takeaways: the slide's limitations section (`review-limitations.md`) shows them, so do not repeat them here.

Write ONLY `<review>/data/explainers/synthesis.json`:

```
{"takeaways": [
  {"text": "two to four short sentences",
   "detail": "40–80 words: the numbers, ranks and papers behind the takeaway",
   "emphasis": ["key phrase from text"],
   "rationale": "which clusters, KPI ranks or notes in the packet support it",
   "cluster_ids": ["C001", "C002"],
   "evidence": [{"paper_id": "P00x", "quote": "copied verbatim from that paper's note in the packet", "location": "..."}]}]}
```

## Rules

- Every takeaway needs at least one evidence quote, copied character for character from the packet.
  `merge-synthesis` drops quotes that are not in the paper's cached text and drops a takeaway left with none.
- `emphasis` is what the slide highlights: 2 phrases per takeaway, 1 when the `text` has fewer than 40 words (the
  hub and REVIEW.md show no more: the longest first), each copied character for character (same case) from its own
  `text`, at most 6 words each: the comparison or finding a reader who only skims must not miss.
  Never a whole clause list, never a bare number or name on its own. `merge-synthesis` drops a phrase that is not in
  its `text`.
- KPI numbers you cite must be the packet's numbers. No ranking claims beyond the KPI boards.
- Every count of papers, members or families comes from `counts`: never count the lists yourself (writers who did
  wrote "7 of the 13 ranked papers" for 12 and "16 papers in these four families" for 15). `merge-synthesis` warns
  about a count that does not match what it counts.
- Claims about code or weights come only from the members' `availability` fields, never from what the method
  notes or quotes happen to mention. `unknown` is not "no": say "no release found", never "not released" or
  "only X has code". Quotes stay method-note quotes; a code URL is not evidence for a takeaway.
- Plain words in the style of `ste-style.md`; define any technical term in the sentence that uses it.
- Name each family by its cluster `name` from the packet, or by its C-id next to a short form of that name.
- Return nothing else.
