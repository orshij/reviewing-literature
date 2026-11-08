# Add emphasis (step 6)

You choose the words the review's Explainers tab highlights in its long texts: the slide fields, the
limitations and weak points, the synthesis takeaways, and the method notes (data, output, loss, architecture,
training) of each slide's paper that its "In this review" strip shows. The step-6 writers mark their own texts;
this pass adds the marks to texts that have none (reviews made before the writers did, a writer that left them
out, and the method notes, which no writer marks).

## Inputs (given in your dispatch message)

- `review`: the review folder; `litrev`: the path to `litrev.py`.
- `packet`: `<review>/data/explainers/emphasis.input.json`, written by `litrev.py emphasis-input`. It holds the
  review's `purpose`, the limits `max_phrases` and `max_words`, and `texts`: a list of `{id, text}`.

## Task

Read ONLY `packet`. Do not rewrite any text: you only pick phrases from it.

Write ONLY the packet's `output_path` (`<review>/data/explainers/emphasis.json`): one entry per text id in the
packet, nothing else.

```
{"C003.problem": ["phrase copied from that text"],
 "C003.weak_points[2]": ["...", "..."],
 "synthesis[0]": ["...", "...", "..."],
 "P323.loss": ["...", "..."]}
```

## Rules

- 1 phrase for a text under 40 words, 2 for a longer one (the hub and REVIEW.md show no more: the longest first;
  `max_phrases` is only the hard limit `merge-emphasis` keeps). Each phrase is copied character for character (same case, same punctuation) from that text and has at
  most `max_words` words. `merge-emphasis` drops a phrase that is not in its text.
- Mark the words a reader who only skims must not miss: the claim, the mechanism, the failure, the condition or
  the contrast. Prefer the phrase that answers the review's `purpose`.
- Never a whole sentence, never a bare number, paper name or dataset name on its own, never filler ("in this
  paper", "the authors").
- Two phrases in one text must not overlap.
- A method note (id `P###.<field>`) shows only its first sentence until the reader opens it. When that first
  sentence holds a key point, take one phrase from it.
- When a text has nothing worth marking, write `[]` for it.
- Before you finish, run `python3 <litrev> check-json <your file>` and fix any error it reports.
- Return nothing else.
