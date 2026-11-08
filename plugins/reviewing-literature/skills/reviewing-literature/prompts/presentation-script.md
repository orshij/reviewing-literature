# Presentation script (step 8): one chapter

You write the narration and the scene list for ONE chapter of a narrated explainer video of a literature review. A
voice engine reads your sentences aloud and the captions show them; a renderer draws each scene's visual from the
review's data and reveals its parts when the narration reaches them. The chapter plan (`presentation-plan.md`) already
fixed the chapter's question, evidence, visual, engagement and takeaway: follow it.

## Inputs (given in your dispatch message)

- `review`: the review folder; `litrev`: the path to `litrev.py`.
- `packet`: `<review>/data/presentation/script/CH##.input.json`, written by `litrev.py script-input --chapter CH##`.
  It holds the `target` (words per minute, audience), your `chapter` card with `duration_s`, the `words_budget`, the
  plan `outline`, the `previous` chapter (its transition leads into yours) and the `next` one (end on its question),
  the `headline_answer`, the `recurring_example`, the `engagement` the plan gave you, your chapter's `claims` (the
  points it states), `claims_elsewhere` (the points other chapters state), `said_elsewhere` (the narration of the
  chapters merged so far), a `review` summary, the `records` your chapter cites (family explainers with their diagram, papers with their method notes, result rows
  with values as printed, KPI boards, limitation problems, takeaways, report items), the `visual_options` you may show
  (each with the `element_ids` its reveals may name), the `allowed_numbers`, the `terms` a voice may misread and the
  review's `lexicon` so far. `review.sections` names the review sections your chapter covers (of the ten in
  `presentation-plan.md`), with their questions and data scenes; `review.summaries` holds the tasks, test sets and
  metrics tables; `review.report` the parts of the review report your sections use (executive summary, verdict,
  requirements, unmeasured, conditions, gaps, roadmap); and, when your sections need them, `review.search`, `review.provenance`,
  `review.weak_points` and `review.feasibility`.

## Task

Read ONLY `packet`. No web, no full texts, no other chapter's files.

**Voice.** Write natural spoken English, like a knowledgeable researcher explaining the review to a colleague: plain
words, natural contractions, a warm and professional tone, lightly playful only where it helps an idea land. **This
text does not follow `ste-style.md`**: spoken narration needs contractions and conversational phrasing.

**One idea per sentence.** A listener hears each sentence once and cannot re-read it. A sentence that carries a name,
a definition, a number and a caveat loses all four.

- One sentence says one thing: a claim, one step of a mechanism, one result, or one caveat. Most sentences have 6–14
  words; none has more than 18. Fragments are fine when they are spoken ("Ten extra runs per question.").
- A definition gets its own sentence. Define an unfamiliar term the first time you use it, then keep that word.
- At most one result number per sentence, and at most one name (method, model, dataset) the viewer has not heard yet.
  After a name's first mention, call the method by its role ("the trained judge") unless two are being compared.
- No appositive stacked on a noun, no aside in parentheses or between dashes, no spoken list of more than 3 items. A
  longer list goes on screen or into the visual's reveals; the narration says how many there are and names the one
  that matters.
- The point first, then the evidence: "Camera-aware networks win in meters. The best misses by 4.2%." Not the evidence
  first and the point at the end of a long sentence.
- Signposts tell the viewer what comes next: "Here's the surprise." "So what does that mean?" "One thing limits this
  result." Vary them; never the same one twice in a chapter.

Before and after (from earlier reviews; the facts stay the same, only their order and packaging change):

- Before: "With a heading prior on KITTI Test-2, fully supervised CROSS has the lowest median position error of 14
  methods: 1.82 metres." After: "CROSS has the lowest error of all 14 methods. Its median miss is 1.82 metres."
  `on_screen`: "KITTI Test-2, heading given".
- Before: "MoGe-3 leads 3 boards, Modality Forcing leads NYUv2, and PointDiT, trained only on synthetic images, ranks
  2 to 4 on all 4." After: "MoGe-3 leads 3 of the 4 boards. Modality Forcing leads on NYUv2. And one result stands
  out. PointDiT trained only on synthetic images. Yet it ranks 2 to 4 on every board."
- Before: "DepthMaster, FoundationGeo, PXDepth and MoGe-2 also make the ETH3D top ten." After: "Four more from this
  family make the top ten." The chart's reveal shows the four with their ranks.
- Before: "So teacher labels can top a metric board, but with two papers and test-set tuning, that's a promising
  signal, not an established result." After: "So teacher labels can top a metric board. But only two papers show it,
  with some tuning on test sets. That's a promising signal, not an established result."

**Say each thing once** (the user, 2026-10-28: "do not repeat sentences and even subjects/claims"). The viewer heard
it: a sentence, a claim or a caveat said again in other words costs time and sounds like padding.
- Inside your chapter, no sentence says again what an earlier one said: no preview of the chapter at its start, no
  recap at its end, and the key message once, where it is earned.
- The previous chapter ends by asking your chapter's question (`previous.transition`): do not ask it again. Start on
  the first step of its answer, or on your card's `opening`. Your own last sentence leads into `next.question` once;
  the next chapter will not repeat it.
- Across the video: do not state a point from `claims_elsewhere` or a sentence of `said_elsewhere` again. When your
  point builds on one, refer to it in a few words ("with that caveat in mind") and say what is new. State your own
  `claims`, each once. Cite another chapter's claim in `sources` only in the executive summary, which summarises them.
- The executive summary is the top-down answer, your chapter is the detail under it: give the mechanism, the numbers
  and the limits that the summary could not, never its verdict again.
- `check-script` lists every sentence said before under `repetition`, with the sentence it repeats: cut it, or say
  what is new instead.

**Say fewer things, and explain them well.** The plan card and the packet hold more facts than a listener can take in:
choose. Explain the chapter's question with at most 3 pieces of evidence said aloud. Say aloud the qualification that
changes the chapter's conclusion (the card's `comparability`), as the limitation itself, in one or two sentences; show
the other caveats as short `on_screen` lines without pointing at them, or leave them to the confidence chapter and the
hub. Never drop a caveat that would change the claim the scene makes. A fact you leave out is not lost: the hub holds
the full tables.

**Words.** The merge lists these as `wording` notes, like the density notes:
- "board": say "comparison", "test" or the test set's name. A board blurs the review's own comparison and a public
  leaderboard.
- "best paper": say "representative paper". The first family chapter says once how it is chosen (the best-ranked
  member with a published venue); when another member reports a higher score on the comparison shown, say so.
- "catch", "here's the catch", "N more ... on screen": say the limitation itself.
- "just", "only", "merely" before a number ("adds just 4.32 points"): state the increment; a judgement of its size
  needs the decision criterion, which you say, or you leave the judgement out.
- "teacher training": name the mechanism (the teacher writes worked examples; the teacher grades answers as rewards).
- Record ids (C005, P1961, K010, X2, CN4) never appear in narration or `on_screen`: the merge refuses them. Say the
  method, family or experiment by name ("the second experiment").

**Rank words.** Say "the highest reported score" when the papers on a comparison start from different base models or
settings. Say "ranks first" only when the comparison's label fixes the base model and the setting (for example "LLaMA-2-7B
with 50% of weights removed"). The video's chart shows no rank badges; its bars keep the order.

**Spread.** When a cited result has a `spread` ("pm", "runs"), show it with the value ("54.35 ± 0.50 over 3 runs") or
say it once. A claim about missing spread must hold for every paper it covers: a paper can report a spread in one table
and one value in another, and repeated runs with one seed are not a spread across training seeds. Say which one a
paper gives.

**Intuition before detail.** Open a difficult idea with a concrete example or with the visual, then give the term,
the mechanism and the evidence. For a method, follow input → representation → mechanism → output, one step per
sentence, and add the training objective or supervision when it explains the result. Explain each symbol or component
before you compare with it.

**Beats** (adapt them; do not make every chapter sound the same): open the way the card's `opening` says (a question,
a surprising result, a concrete failure, a changed constraint, a prediction, an example, a contrast), then the
explanation (30%), the evidence (30%), the qualification that matters (15%), the takeaway and the transition (15%).
Say the takeaway once: when an earlier sentence already said it, end on the transition. A family chapter ends on what
is new in it (the component that transfers, the result that limits the family, the test it needs), not on caveats
that other chapters claim. A conditional takeaway fits when the evidence supports one ("promising under these
assumptions; untested in that setting"); then lead into `next.question`.

**The recurring example.** `recurring_example.here` says its role in your chapter. `introduce`: present it once,
plainly, with its concrete input and the output it needs; say once that it is invented. `revisit`: one short return,
only to say how this chapter changes its decision; do not introduce it again and do not call it invented (the screen's
Illustrative badge does that). `none`: leave it out of this chapter entirely. Other analogies stay allowed.

**Length.** About `words_budget` words in total, and never more than 5% over: a chapter that runs long makes the whole
video long. Fewer facts do not mean fewer words: spend the words on explaining the mechanism and the evidence, and on
signposts, not on more records and not on saying a point again. The merge estimates speech time from the text the voice will say (each name respelled by the
lexicon counts as the words it is said with: "QPP" is 3, "Qwen3-32B" is 5) at the voice's measured rate, plus the
0.3 s between sentences and the pauses. It refuses a chapter more than 20% off its duration.

**Scenes.** 3–10 scenes; start a new scene when the visual changes. Each scene:

- `id`: `CH##-S01`, `CH##-S02`, ...
- `narration`: a list of sentences, one per item, in caption form.
- `visual`: one entry of `visual_options` (its `kind` and `refs`), plus `reveal`: steps `{"at": <sentence index from
  0>, "show": [<ids from that option's element_ids>]}`, in the order the narration explains them. Show and highlight
  what the narrator is talking about, when they talk about it. For `claim` and `example` visuals the ids are line
  indexes of `on_screen`. A `results-table` option lists its `columns` (`col1`, `col2`, ...): name the 1-3 columns
  whose values you talk about in a reveal, and the table draws only those (D-Pruner's eleven columns were on screen
  while the voice compared two values). A `matrix` draws only the rows its reveals name, and a `roadmap` scene at most
  three experiment cards. Each scene is drawn from a blank screen: an element appears only when a reveal names it
  (until then a bar or a table row is hidden, other elements are faint), so reveal at sentence 0 what must be on
  screen from the start, again in every scene that shows it. An empty `reveal` shows only the visual's frame (title,
  axes, headers): use it for a prediction question, then reveal the answer in the next scene. `claim` and `example`
  lines that no reveal names show from the start. A `kpi-chart` draws only the papers its reveals name, in score order
  with no rank badge, so name the papers the narration compares, at most 12 in one scene, and say the setting that
  differs between them before the scores.
- `on_screen`: short lines (≤ 8 words each, one per line), in most scenes: what the voice leaves out or rounds - the
  exact number with its setting, a full model name, a list, the caveats not said aloud - or the scene's conclusion.
  Not a transcript of the narration. On a chart, table, diagram or figure the lines show together in a strip under
  the visual: 1–3 lines there, and at most 2 short ones on a `matrix` (a third line takes a second row and shrinks
  its rows); up to 5 on a `claim` or `example`.
- **Data scenes.** When your `chapter.summaries` lists kinds, give each one a scene of its own whose visual is that
  kind (`dimensions` takes 1-3 paper ids and `standing` one cluster id from `visual_options`; the others take no refs). The renderer draws each from
  the review's data; you choose only the reveals and what the voice says. The merge refuses the chapter when one of
  its data scenes has no scene. Per kind:
  - `tasks`, `benchmarks`, `metrics`: the whole table shows from the start (the rows of `review.summaries.<kind>`,
    each with its number of included papers and a KPI tag); a reveal highlights the rows its sentence names. Say aloud
    at most 3 rows, with their counts as the table gives them, and one sentence on what the table means (the task most
    papers work on, the test sets the KPI boards use, how close they are to the target, what no paper measures). On
    `benchmarks`, say once what separates a benchmark (a task and its rules on a dataset) from a dataset. Cite the
    rows' ids (`T`/`D`/`B`/`M`) as `sources`.
  - `executive-summary` and `verdict` (section 1): the top-down summary, one connected story: the problem, then the
    verdict, then the main uncertainty and the first step (the user, 2026-10-28: "start with top down summary (which
    is better on what principles) before getting into details for each family"). On the `verdict` scene reveal one row
    per step and say, for each: the goal, the approach that serves it better, and the principle that makes it better,
    in plain words, with its condition; give the support in a few words ("a handful of papers", "consistent across the
    comparisons", "not tested yet"). Use the `executive-summary` scene (parts `problem`, `uncertainty`, `next_step`)
    before and after it. **Name no paper and compare no papers** (the user, 2026-10-26): the approaches are the
    actors, and no exact score is needed: the method chapters give them. A concrete case helps where a principle is
    abstract. Do not say a row twice, and do not end on a recap.
  - `requirements` and `scope` (section 2): the application goal, the task, the constraints and the research
    questions; then what is in and out of scope and the comparison rules. Say plainly which parts the review does not
    state.
  - `search` (section 3): about 45 seconds. What was searched and the last search date, how studies were selected and
    the main coverage limit. Counts, caps and walks stay on screen. Say that the search was selective, not systematic.
  - `standing` (section 6): **the chapter's first scene**, before the family's idea is described (the user,
    2026-11-01: "show for each line of work the place in the leader boards before its description"; the merge refuses
    the chapter otherwise). The screen shows every comparison side by side: its leaders, then this family's members,
    each with its rank of the comparison's papers (`review.standing`: `best` is the family's best rank, `of` the number
    of papers). Reveal one comparison per sentence you say about it. In 2-3 short sentences say where the family stands:
    its best rank on each comparison you name ("sixth of forty-five on WTQ"; call a comparison by the name its label on
    the screen starts with, the part before " · ", in every family chapter, so the viewer hears one name for it), and whether it leads, sits in the
    middle or has no result there. Say no score (the screen has them) and no verdict: why the family is placed there
    is what the rest of the chapter explains. A rank compares the highest reported scores, whatever the base model:
    the screen says so; say it aloud only in the first family chapter. Cite the KPI ids as `sources`.
  - `dimensions` (section 6): the representative paper's data, representation, architecture, losses and training, one
    reveal per dimension, from its method notes in `records`. Say which component could transfer to the target system even
    if the whole method does not fit. With two papers, say how the later one changes the idea. Do not say the family's
    verdict row again: the summary gave it; this scene shows how the method is built.
  - `unmeasured` (section 5), `conditions` (section 7), `gaps` (section 9): the report's items, one reveal per item
    you say. A condition is "works when" or "unresolved"; say why an unresolved comparison stays open. Name a gap's
    type (method limitation, evidence gap, engineering gap, research opportunity) and phrase it inside the review's
    coverage: "The reviewed studies do not establish ...", never "nobody has studied". A gap the screen marks as
    general knowledge is said as such.
  - `provenance` (section 7): where each ranked number comes from: reported by the paper, verified on an archived
    leaderboard, reproduced (none: a review runs nothing). Say what that means for how much to trust the ranking.
    `weak-points` (optional) shows the kinds of weak point the reviewer found in each family's best paper: the
    reviewer's reading, never the authors' words.
  - `feasibility` (section 8): two questions, answered apart: can a team evaluate the published model (the authors'
    weights, their licence and size), and can it retrain or adapt it (the code)? Weights with a standard loader can be
    evaluated without code: never say such a model "would have to be rebuilt". "Not found" is not "none": say "no
    release found". Say what the review does not assess (compute, domain fit, the training data release).
  - `roadmap` (section 10): the baseline first, the shortlist, then each proposed experiment with the question it
    resolves, what it measures and the decision it supports; at most three experiments per scene. Say "we propose", never "we ran" or "the results show": nothing here was
    run. The screen labels it "Proposed experiments, not conducted".
- `claim_type`: `reported` (a study's finding; needs `sources`), `synthesis` (your reading across studies; cite the
  records), `action` (a proposed next step: every roadmap scene), `illustrative` (an invented example or analogy: the
  screen shows an Illustrative badge; the narration says it only where the recurring example is introduced, or once
  for an analogy).
- `sources`: the record ids the scene's claims rest on (`P`/`R`/`K`/`C`/`L`/`TK` ids from `records`; `T`/`D`/`B`/`M`
  ids from `review.summaries` on a summary scene; the report's `U`/`CN`/`G`/`X` ids on its scenes).
- `pause_after_s`: 0.3–1.0 normally; 1.5–3 after a prediction question or before the viewer must read a dense visual.
- `transition`: `cut`, `fade` or `none`. `engagement`: `null`, or the plan's engagement on the one scene that asks it.

**Numbers.** Write every number above 12 as digits ("0.056", "35 papers", "2024"), never as words, so the check can read it;
a small count may be a word ("three families"). A model size keeps its unit as the records write it ("3.52B" may be said
"3.52 billion").
Use only numbers in `allowed_numbers` or in the records you cite, as printed or rounded (0.056 → "about 0.06").
Give a number's dataset, split, setting and model version in the same scene: on screen, or in the sentence before it. Never invent an error bar, a percentage or a
failure rate. The merge refuses a chapter with a number that is in no cited record (small counts up to 12 pass).
Say a KPI result as its `shown` form ("4.4%"), the scale the video's chart draws for that board, not its stored
`value`: papers store one metric as a fraction or in percent, and one board gets one scale.

**Engagement.** Use only the moment in `engagement`, on one scene; add none of your own. For a prediction: ask the
question, pause (`pause_after_s` ≥ 1.5), then give the supported answer and the reasoning, or say what would settle it.

**Analogies and humor.** An analogy says where it stops being accurate, and the narration returns to the actual
mechanism before it draws a conclusion. Humor is optional, brief and accurate; never at a researcher's expense, never
an invented finding.

**Pronunciation.** Add `{"term": "...", "say": "..."}` only where the written form would be read wrongly: an acronym
said as a word ("TREC" → "trek"; an all-capitals acronym said letter by letter needs no entry, the voice spells it),
a name with digits or odd capitals ("DUSt3R" → "duster", "MuSiQue" → "music", "Qwen3-8B" → "kwen three, eight B"), or
a symbol ("MAP@100" → "map at one hundred"). Never respell an ordinary word, a Greek letter ("tau") or a number: the
voice reads those. Keep every `lexicon` entry as it is, so that one name sounds the same in every chapter. The
narration keeps the written form; the audio step applies the respelling.

Write ONLY the packet's `output_path` (`<review>/data/presentation/script/CH##.json`):

```
{"schema": 1, "chapter_id": "CH03",
 "scenes": [
  {"id": "CH03-S01",
   "narration": ["First sentence.", "Second sentence."],
   "visual": {"kind": "diagram", "refs": ["C002"], "reveal": [{"at": 0, "show": ["img"]}, {"at": 1, "show": ["enc", "dep"]}]},
   "on_screen": "",
   "claim_type": "reported", "sources": ["P012"],
   "pause_after_s": 0.6, "transition": "cut", "engagement": null}],
 "pronunciations": [{"term": "DUSt3R", "say": "duster"}]}
```

## Rules

- Ids exactly as the packet writes them; a source must be a record id in `records` or `review`.
- The merge also refuses a reported scene with no source, an unknown visual or ref, a reveal past the last sentence or
  of an element the visual does not have, and an `example` visual on a scene that is not `illustrative`.
- Before you finish, run `python3 <litrev> check-script --review <review> --chapter CH##` (the merge's own checks; it
  stores nothing) and fix every error it lists; read its warnings too. Each of its `density` notes names a sentence
  that carries too much (over 18 words, or several clauses, names and numbers): split it or move a part on screen. Each
  `repetition` note names a sentence that says again what your chapter or another one said, or a scene that cites
  another chapter's claim: cut it, or say what is new. Reveal ids may be strings or integers.
- Return one line: the number of scenes and words, and any number or claim you left out for lack of a source.
