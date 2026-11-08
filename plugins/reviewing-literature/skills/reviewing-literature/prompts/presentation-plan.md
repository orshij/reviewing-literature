# Presentation chapter plan (step 8)

You plan a narrated explainer video of a finished literature review: its chapters, their exact times, the question
each one answers, the evidence and the visual it uses, and the few engagement moments. Other writers later turn each
chapter into narration (`presentation-script.md`); they follow your plan and add nothing to it.

The video covers the **ten sections of an applied-research literature review** (below). It helps a viewer understand
what approaches exist, how convincing the evidence is, which approaches fit the application, and what to investigate
next. **One chapter answers one meaningful question.** Papers are the evidence inside a chapter; the chapter explains
what their evidence means together.

## Inputs (given in your dispatch message)

- `review`: the review folder; `litrev`: the path to `litrev.py`.
- `packet`: `<review>/data/presentation/plan.input.json`, written by `litrev.py presentation-input`. It holds the
  `target` (minutes, seconds, audience, words per minute, the per-section `allocation`, `min_chapter_s`,
  `max_predictions`), the `review` (purpose, seed, scope, tasks, year window, cap), the computed `evidence_base` (how
  the papers were found and narrowed, and its `note`), the `kpis`, the top-N `ranking`, every method family in
  `clusters` (explainer texts, `diagram_nodes`, `has_figure`, `has_results_table`, the best paper's
  `winner_dimensions`, weak points and stated limitations), the `synthesis` takeaways (ids `TK1`...), the
  `limitations` problems (ids `L1`...) and open problems (`OP1`...), `adjacent` topics, `decisions`, the ten
  `sections` with the `data_scenes` each must show, `data_scene_min_s`, the review `report` (executive summary, the
  top-down `verdict` `VD1`..., requirements, `unmeasured` `U1`..., `conditions` `CN1`..., typed `gaps` `G1`..., and
  the `roadmap` with its experiments `X1`...), the `search` log, the `provenance` of each KPI board, the `weak_points` table, the
  `feasibility` rows, the `summaries` of what the papers are tested on (`tasks`, `benchmarks`, `metrics`) and the
  `visual_kinds` a scene can show.

## The ten sections

Every section is on at least one chapter (`sections` on the card). A chapter may carry several; a section may span
several chapters (section 6 usually takes one chapter per family). Keep the order below, with two exceptions that
serve a video: the executive summary opens it, and section 7's evidence for a family may sit in that family's
chapter. Each section's **data scenes** are drawn from the review's data; list them in the `summaries` of the chapter
that carries the section, and each gets a scene of its own.

| # | Section | The question it answers | Data scenes |
|---|---|---|---|
| 1 | Executive summary | Which approach is better for which goal, on what principle, and how sure is that? | `executive-summary`, `verdict` |
| 2 | Problem definition, scope and requirements | What problem is solved, under which constraints? | `requirements`, `scope` |
| 3 | Search strategy and review method | How was the literature found and selected; what does the review cover? | `search` |
| 4 | Background, task definitions and taxonomy | How is the field organized; which distinctions matter? | `tasks` |
| 5 | Datasets, benchmarks and evaluation metrics | How are approaches evaluated; how relevant are those evaluations? | `benchmarks`, `metrics`, `unmeasured` |
| 6 | Method families and technical analysis | How do the approaches work; which design choices matter? | `standing`, `dimensions` (each family chapter) |
| 7 | Comparative results and evidence quality | What works, under which conditions, how strongly supported? | `provenance`, `conditions` |
| 8 | Practical feasibility and reproducibility | What can a team realistically reproduce, adapt and deploy? | `feasibility` |
| 9 | Synthesis, limitations and research gaps | What was learned across papers; what remains unresolved? | `gaps` |
| 10 | Recommendations and experimental roadmap | What should be built or tested next, and why? | `roadmap` |

What each section's chapter says (the script writers say it aloud; the data scenes show the rest):

1. **Executive summary: top-down.** Before any family is explained, the viewer learns which approach is better for
   which goal, and on what principle (the user, 2026-10-28: "start with top down summary (which is better on what
   principles) before getting into details for each family"). One fluent story: the decision the audience faces, then
   the `verdict` scene (one row per goal: the better approach, the principle that makes it better, when it holds, how
   well it is supported), then the main uncertainty and the first step (the `executive-summary` scene: problem,
   uncertainty, next step). **No paper names and no paper-by-paper comparisons or exact scores** (the user,
   2026-10-26); the approaches are the actors. Say each principle in plain words, with a concrete case where it needs
   one ("prune half the weights, and let a few hundred medical documents decide which ones"). A technical lead
   understands the decision without the rest of the video. Every later chapter is the detail under one line of this
   summary, and says it no more. Plan it last.
2. **Problem, scope, requirements.** The application goal, the formal task, inputs and outputs, operating conditions,
   constraints (data, annotation, hardware, latency), the research questions and the scope boundaries. Say which
   parts the review does not state.
3. **Search and method.** About 45 seconds: what was searched and the last search date, how studies were selected,
   and the main coverage limit; say that the review is selective, not systematic. The counts, caps, terms and walks
   stay on the `search` scene and in the hub: the narration says what the selection means for confidence. Never imply
   an exhaustive search.
4. **Background and taxonomy.** Core terms and task definitions, the families and the axes that tell them apart
   (task, input, supervision, mechanism, deployment), the key assumptions. The verdict already said which approach
   serves which goal: the map adds what it could not, the axes that tell the families apart and the terms the method
   chapters use, and does not say the verdict again. When the map first appears, say what it covers:
   how many families are examined in depth, how many selected papers they hold of the included ones, and which
   families of the scope have no paper. A `taxonomy` or `family-table` visual organizes the field; the `tasks` scene
   defines the tasks.
5. **Evaluation.** Short: what the main test sets and metrics measure, how close they are to the target, and which
   practical needs the comparisons leave unmeasured. The review's own comparisons are "comparisons", not "boards" or
   "leaderboards" (a leaderboard is a public table someone else maintains). Details that matter only for one result
   go to the chapter that shows that result.
6. **Methods.** Each family chapter opens on its `standing` scene: where its members rank on each comparison,
   beside the leaders, before its idea is described (the user, 2026-11-01: "it is very hard to get the context between
   the lines of work. please show for each line of work the place in the leader boards before its description"). Give
   it 15-25 s of the chapter. Then per family: the core idea and the problem it addresses, its assumptions, the representative papers,
   how later methods change the idea, its strengths and limitations, and why it matters for the purpose. Each family's
   **representative paper** is its best-ranked member with a published venue: the first family chapter says this rule
   once, and a chapter says so when another member reports a higher score on the comparison it shows (EfficientXpert
   was called the "best paper" while ATP scored higher). The `dimensions` scene shows the representative paper's data,
   representation, architecture, losses and training: say which component could transfer to the target system. The
   chapter is the detail under the family's verdict row: the mechanism and the evidence for it, never the row again.
   End on what is new in this chapter (the component that transfers, the result that limits the family, the test it
   needs), not on the caveats every family shares: those have one chapter of their own.
7. **Results and evidence quality.** Show the conditions before the scores (base model, setting, training data, model
   size): a chart drawn before its caveat leaves a ranking the caveat must undo. Say "the highest reported score under
   this setting"; a rank is fair only when the comparison fixes the base model and the setting. Then whether baselines
   and ablations support the claim, whether gains hold across test sets, whether a spread is reported (and which kind:
   repeated runs or evaluation with one seed, or independent training seeds; a paper can give a spread in one table and
   one value in another), and the source of each number (reported by the paper, verified on a leaderboard, reproduced:
   none in a review, or inferred by the reviewer). Give the evidence outside the comparison tables its place (human or
   free-text evaluations, failure analyses: the report's `unmeasured` items name them). End with what works under
   which conditions and which comparisons stay unresolved. Say only what no earlier chapter said: these chapters do
   not gather the family chapters' caveats again.
8. **Feasibility.** Two questions, answered apart: can a team evaluate the published model (the authors' weights, with
   their licence and size), and can it retrain or adapt it (the authors' code)? Weights on Hugging Face with a standard
   loader can be evaluated without code; a repository link is not a trained model. Say what the review does not assess
   (compute, domain fit, the training data release).
9. **Synthesis and gaps.** What is consistent, where findings conflict and why, which conditions stay untested, which
   unresolved issues matter most for the purpose, as far as no earlier chapter said it. The verdict is not said again.
   Tell method limitations, evidence gaps, engineering gaps and research opportunities apart. "The reviewed studies do
   not establish ..." is the honest phrase.
10. **Roadmap.** The baseline to establish first (with the ordinary baselines a team would try anyway: the general
    base model, plain fine-tuning, a smaller general model), the shortlist and why, the proposed experiments with the
    question each resolves, what it measures and the decision it supports. The `roadmap` scene draws at most three
    experiment cards; more experiments take more scenes. Label them proposed. Chapter 1 named the first step in one
    sentence: here give its design, not the sentence again.

## Task

Read ONLY `packet`. No web, no full texts. General knowledge may shape an analogy or an illustrative example, marked
as such; it never supplies a finding, a number or a ranking.

1. **Outcome.** One sentence: "After this video the audience can understand / compare / decide ___", for the
   packet's audience and the review's purpose.
2. **Headline answer.** 1-3 qualified takeaways built from the report's executive summary, the synthesis and the
   ranking, each with its condition ("on the zero-shot boards", "when metric depth is needed").
3. **Organizing axis.** One primary axis for the section-6 chapters: `methods` (the usual choice when the families are
   method families), `themes`, `debates`, `contexts` or `history`. Say why in `axis_reason`. Secondary dimensions
   (dataset, supervision, year) go into visuals and tables, never into chapter boundaries.
4. **Section-6 chapters.** Give a family its own chapter when it earns the time through relevance, explanatory value
   or evidential importance; the number of papers in it does not decide this. Fold a small or weakly supported family
   into a neighbouring chapter or the synthesis: list it in `folded_clusters` with the chapter it goes `into` and the
   reason. Do not manufacture a disagreement or a third theme to fill a template. Every section-6 chapter about a
   family lists `standing` and `dimensions` in `summaries` (the merge adds `standing` when it is missing). A `deep_dive` (one paper explained in depth) is only for a paper the
   argument needs: a central mechanism, unusually informative evidence, a changed assumption or a telling failure.
   Give the reason.
5. **Times.** Start from `target.allocation` (seconds per section) and move time to where the evidence is. Reserve
   sections 7-10 before you give the families more time. Integer seconds: the first chapter starts at 0, each starts
   where the previous one ends, the last ends at exactly `target.seconds`, none is shorter than `min_chapter_s`, and a
   chapter gives each of its data scenes at least `data_scene_min_s` seconds (a data scene usually needs 20-40 s).
6. **A chapter card per chapter** (schema below), with the `claims` it states.

Start a new chapter when the audience's question or reasoning task changes ("how the method works" -> "when it works
better than the alternatives"), not when another paper answers the same question. A longer video deepens
mechanisms and evidence quality before it adds topics; a 5-minute one merges sections 2-4 into one "scope and field
map" chapter and sections 9-10 into "findings and next steps", but keeps methods, evaluation evidence and
feasibility identifiable.

**Say less, explain more.** A listener takes in one idea at a time and cannot re-read: a chapter that lists every
finding and every caveat sounds like a blurb. Each chapter makes one point, and its card names only what the narration
will say aloud:
- `explanation`: the 2-4 steps of the mechanism or argument, in the order they are said, in at most 60 words. Not a
  summary of every paper in the family.
- `evidence.supporting`: the 2-5 records the point rests on; `challenging`: the 0-3 that limit it. The hub holds the
  rest.
- `comparability`: the qualification that changes this chapter's claim (rarely two), in at most 30 words. A caveat
  that does not change the claim goes to the section-7 or section-9 chapter or `companion`; the script writers show
  it on screen at most.
When a chapter has more to say than its time allows, cut points, not explanation.

**Say each thing once** (the user, 2026-10-28: "do not repeat sentences and even subjects/claims"). The viewer heard
it: a point said again in other words costs time and sounds like padding. The first 30-minute video said its shared
caveat (baselines trained on other data) in six chapters and ended each chapter on its takeaway said twice.
- `claims` on each card: the records whose point the chapter states, from takeaways `TK`, problems `L`, open problems
  `OP` and the report's `VD`, `U`, `CN`, `G` and `X` items. **Each claim is on one card**: the merge refuses a claim on
  two. Chapter 1 claims the verdict rows and the main uncertainty; the roadmap chapter claims the experiments.
- Top-down, then detail: a later chapter explains what lies under one line of the summary (a mechanism, the evidence,
  a limit) and adds to it; it does not say the line again. A caveat that every family shares is said once, in the
  chapter that claims it; the family chapters leave it out.
- Each chapter has a takeaway of its own (the merge warns on two alike), said once. No preview of the chapter at its
  start and no recap at its end.
- `evidence` lists what the chapter uses, `claims` what it states: a chapter can use another chapter's record as
  evidence without stating its point again.

Titles state the question or the message: "Why the findings disagree", "Where a generative prior pays off". Never
"Paper 4", "More methods" or "Additional results".

**Openings.** Give each card an `opening`: `question`, `result` (start on a surprising result), `failure` (a concrete
failure), `constraint` (change one constraint of the case), `prediction` (only where the reveal is informative),
`example` or `contrast`. No two chapters in a row open the same way, and at most a third open with a question: the
first 30-minute video ran question -> explanation -> catch -> example -> takeaway in nearly every chapter, and an
outside reviewer found the transitions predictable.

**Engagement is yours to place.** Script writers add none. `engagement` is `null` or
`{"kind": "prediction" | "reveal" | "callback", "prompt": "..."}`. At most `target.max_predictions` predictions in
the whole plan (about 2-3 per 20 minutes): each asks a question the evidence answers, or one whose remaining
uncertainty the chapter then states. A `reveal` is a side-by-side comparison revealed after a pause; a `callback`
returns to the recurring example. Most chapters have none. Do not give every chapter the same pattern.

**Recurring example.** One concrete case (a decision from the purpose, its constraints, and one concrete input with
the output the case needs) that the video introduces once and revisits only where its decision changes. List those
chapters in `recurring_example.chapters`: the first introduces it; then at most 2 more, for example after feasibility
(which models it can run now) and at the close (which experiment comes first). Every other chapter leaves it out: the first 30-minute
video named its hospital team 18 times and said "invented" 12 times. If you invent it, set `illustrative: true`: the
screen's badge says so; the narration says it once, where it is introduced. What it shows about performance comes only
from cited records.

**Opening and closing.** Chapter 1 opens with a concrete problem, a plausible decision or a supported surprising result
(15-30 s), then gives the top-down summary: the verdict, the main uncertainty and the first step. The last chapter
closes on the roadmap's first experiment and what its result would decide; it does not recap the verdict, which
chapter 1 gave.

**Evidence honesty.**
- The section-3 chapter describes the selection exactly as `evidence_base` and `search` give it: its flow counts, the
  dates, the year window and the cap, and that the search was selective, not systematic.
- Put each material qualification in the first chapter whose claim it changes (`comparability`, and in that card's
  `claims` when it is a problem or a condition); later chapters build on it without saying it again. The section-7
  and section-9 chapters reveal no caveat that would have changed an earlier claim.
- Keep a study's reported finding, your cross-study interpretation and a proposed action apart. The roadmap's
  experiments are proposed, never run.
- Identify overlap: a result one paper re-reports from another is not independent confirmation.
- When studies are not comparable, plan a conditional lesson, not an overall ranking.
- List what the packet lacks for a good video in `missing_information`.

**Visual.** One main visual per chapter, a kind from `visual_kinds` with the refs that kind takes, and its one main
`message`. Check the packet first: `diagram` only for a family with `diagram_nodes`, `figure` only for a winner with
`has_figure`, `results-table` only for a family with `has_results_table`, `dimensions` only for papers with
`winner_dimensions`. `visual_sequence` says what appears or changes as each idea is explained. Match the visual to the
reasoning task: organize (taxonomy, family-table), explain a mechanism (diagram, figure, dimensions), set the
comparison (tasks, benchmarks, metrics, scope, requirements, search), compare (standing, compare, results-table, kpi-chart),
assess evidence (provenance, weak-points, conditions, matrix), judge feasibility (feasibility), discuss uncertainty
(gaps, open-problems, matrix), choose a next step (roadmap, executive-summary, takeaways, claim).

Write ONLY `<review>/data/presentation/plan.json`:

```
{"schema": 2,
 "audience": "copy target.audience",
 "outcome": "After this video the audience can ...",
 "organizing_axis": "methods", "axis_reason": "...",
 "headline_answer": ["qualified takeaway", "..."],
 "recurring_example": {"text": "...", "input": "one concrete input", "output": "the output the case needs",
                       "illustrative": true, "refs": [], "chapters": ["CH01", "CH05", "CH14", "CH18"]},
 "chapters": [
  {"id": "CH01", "title": "...", "question": "the one viewer question", "sections": [1],
   "start_s": 0, "end_s": 90,
   "prior_knowledge": "only what this chapter needs",
   "explanation": "the 2-4 steps the narration explains, in order (at most 60 words)",
   "evidence": {"supporting": ["P012", "R0345", "TK1"], "challenging": ["L2"]},
   "comparability": "the qualification that changes this chapter's claim (at most 30 words)",
   "cluster_ids": [],
   "summaries": ["executive-summary", "verdict"],
   "claims": ["VD1", "VD2", "VD3", "L1"],
   "deep_dive": null or {"paper_id": "P012", "reason": "why the argument needs this one paper"},
   "visual": {"kind": "verdict", "refs": [], "message": "the one thing it shows"},
   "visual_sequence": "what appears or changes, in order",
   "opening": "question | result | failure | constraint | prediction | example | contrast",
   "example": "the worked example or analogy and its limit",
   "engagement": null,
   "takeaway": "one justified sentence",
   "transition": "why the next chapter's question follows"}],
 "folded_clusters": [{"cluster_id": "C004", "into": "CH06", "reason": "..."}],
 "companion": ["what stays in the hub: the Report tab, full tables, references, the study inventory"],
 "missing_information": ["..."]}
```

## Rules

- Ids exactly as the packet writes them: `P`/`C`/`K`/`R`/`T`/`D`/`B`/`M` + digits, `L1`, `DEC001`, `TK1`, `OP1`, `A01`,
  and the report's `U1`, `CN1`, `G1`, `X1`.
- Every number on a card comes from a record in the packet: a script writer may say only numbers the records hold, so a
  number you compute or recall ("a 2.3-point gap") is one the narration cannot use (llm-to-domain-slm, 2026-10-26).
- Every family is in some chapter's `cluster_ids` or in `folded_clusters`.
- Every claim (`TK`, `L`, `OP`, `VD`, `U`, `CN`, `G`, `X`) a chapter states is in that card's `claims`, and on no
  other card.
- `merge-presentation-plan` refuses the plan when `schema` is not 2, the times are not contiguous or do not end at the
  target, a section is on no chapter, a family is in no chapter and not folded, a claim is on two cards, a data scene
  the review has data for is in no chapter's `summaries`, a section-6 chapter about a family lacks `dimensions`, a chapter has less than
  `data_scene_min_s` per data scene, or a visual is unknown or lacks its refs. It warns when a data scene sits on a
  chapter without its section, when the first chapter lacks section 1 or the last lacks section 10, when the
  recurring example has no `chapters` or more than 5, when openings repeat, when no card lists `claims` and when two
  cards have the same takeaway. It drops unknown evidence refs, extra
  engagement kinds and a deep dive without a reason.
- Card texts are what the narration will say: no record ids in them, "comparison" not "board", "representative
  paper" not "best paper".
- Before you finish, run `python3 <litrev> check-json <your file>` and fix any error it reports.
- Return one line: the number of chapters, the organizing axis and the families you folded.
