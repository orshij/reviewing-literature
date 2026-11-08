# Review report: the ten sections (step 7)

You write the parts of the review report that the review's tables cannot give. The hub's Report tab shows the ten
sections of an applied-research literature review; the narrated video (step 8) uses the same text. The tables already
give the search log, the tasks, the test sets and metrics, the method families with their five dimensions, the KPI
boards and where their numbers come from, the weak points and the code and weights of each family's best paper. You
add the judgement:

| Section | What you write |
|---|---|
| 1 Executive summary | `verdict`: which approach is better for which goal, and why (2-5 rows); `executive_summary`: three short parts around it |
| 2 Problem definition, scope and requirements | `requirements`: the application, the task, the constraints, the questions |
| 5 Datasets, benchmarks and metrics | `unmeasured`: the practical needs that the published benchmarks do not measure |
| 7 Comparative results and evidence quality | `conditions`: what works under which conditions, and what is unresolved |
| 9 Synthesis, limitations and research gaps | `gaps`: each gap with its type |
| 10 Recommendations and experimental roadmap | `roadmap`: a baseline, a shortlist and 2-5 proposed experiments |

## Inputs (given in your dispatch message)

- `review`: the review folder; `litrev`: the path to `litrev.py`.
- `packet`: `<review>/data/report/report.input.json`, written by `litrev.py report-input`. It holds the review's
  `purpose`, `seed` and `scope` (in, out, comparison rules, the person's notes), the year window and the paper cap,
  the `tasks` (definition, inputs, outputs, relevance), the `kpis`, the `summaries` (tasks, test sets with their size,
  origin, access and limitations, metrics with their limitations), the KPI `ranking`, every method family in
  `clusters` (explainer, best paper, its `winner_dimensions`, weak points and stated limitations), the `synthesis`
  takeaways (`TK1`...), the shared `limitations` problems (`L1`...) and open problems (`OP1`...), the `decisions`,
  the `search` log, the `provenance` of each KPI board, the `weak_points` table, the `feasibility` rows (the authors'
  weights with their licence and size, and the code, of each family's representative paper, with a readiness label),
  `outside_tables` (what each representative paper evaluates beyond the KPI comparisons, and `text_read` when the
  text the review read is older than the journal version), the computed `counts`, the `included` papers, the
  `gap_types` and the `condition_kinds`.

## Task

First read `ste-style.md` in this prompt's folder: every text you write follows it. Then read ONLY `packet`. No web,
no full texts, no outside knowledge, except in a gap of type `research-opportunity`, which then cites no reviewed
record and the hub labels it as general knowledge.

1. **Section 1: the top-down summary** (written last, read first). The video opens with it, before any family is
   explained in detail (the user, 2026-10-28: "start with top down summary (which is better on what principles)
   before getting into details for each family"). It is one story in four steps: the problem, the verdict, the main
   uncertainty, the first step. **Name no paper and compare no papers** (the user, 2026-10-26: "make the executive
   summary more fluent, no need to compare and name papers"): the kinds of approach are the actors. No exact scores:
   the method chapters give the numbers. The refs still name the records each part rests on.
   - **`verdict`** (2-5 rows): for each goal of the purpose, which approach is the better choice, and on what
     principle. Each row:
     - `goal`: what the team wants, in at most 8 words ("domain skill on exam-style questions", "a model that needs
       less memory", "adaptation with no labelled data").
     - `choice`: the kind of approach that serves it better, in at most 8 words ("copy a large teacher's worked
       solutions"); `cluster_ids`: its families. When no family serves the goal, say so in `choice`, leave
       `cluster_ids` empty and use support `untested`.
     - `principle`: why it is better, as a mechanism, in at most 25 words: what the approach does that the others do
       not ("Worked solutions carry the reasoning steps; domain text alone carries facts, not steps."). This is your
       reading across the papers: base it on the explainers, the dimensions, the takeaways and the results.
     - `condition`: when it holds, in at most 12 words: the setting, the data or the constraint.
     - `support`: `consistent` (several papers or comparisons agree), `few` (one or two papers), `conflicting` (the
       papers disagree) or `untested` (no comparison measures it; the principle rests on the mechanism only).
     - `refs`: the records the row rests on.
     Give every family a place: a family that is the better choice for no goal still gets a row that says what it is
     for and that the evidence shows no advantage for it (`untested` or `conflicting`). Say which approach is better
     for which goal; do not hedge the table into "no ranking". The support and the uncertainty part say how far to
     trust it.
   - **`executive_summary`**, three parts of 1-3 short sentences each, that tell the story around the verdict without
     repeating a row of it:
     - `problem`: the problem and the decision it poses for the purpose.
     - `uncertainty`: the main uncertainty of the evidence: the one that most limits the verdict.
     - `next_step`: the first experiment of your roadmap, said as what to measure, not by paper name.
2. **`requirements`** (section 2). From the purpose, the scope and the tasks only:
   `application_goal` (the practical outcome), `formal_task` (detection, retrieval, question answering ...),
   `inputs_outputs`, `operating_conditions` (resolution, domain, data conditions), `constraints` (data, annotation,
   hardware, memory, latency, time) and `research_questions` (1-5 questions the review must answer). When the purpose
   and the scope do not give a part, write "not stated in the review". Never invent a constraint.
3. **`unmeasured`** (section 5, 1-5 items). The practical needs of the purpose that the KPI comparisons do not
   measure: latency, memory, cost, robustness, a missing domain, a missing condition. Keep two things apart: what the
   comparison tables leave out, and what no reviewed paper reports. Read `outside_tables` first: when a representative
   paper measures the need outside the comparisons (a free-text grading, a human rating, a case set), say so in the
   item ("The comparisons do not grade free answers. Only the teacher-trained family reports a free-text evaluation,
   graded by a large model."), and cite that paper. When `text_read` says the journal version was not read, say that
   the review read an earlier version. Cite the test sets and metrics (`D`, `B`, `M` ids) or the problems (`L` ids)
   each item rests on.
4. **`conditions`** (section 7, 2-6 items). Which approaches perform well under which conditions (`kind: "works"`),
   and which comparisons stay unresolved (`kind: "unresolved"`: different protocols, too few papers, a re-reported
   baseline). Look at the provenance: all numbers are reported by the papers, so say "reported", never "shown" or
   "proven". Cite the KPI boards, papers, results or takeaways (`K`, `P`, `R`, `TK` ids). Say "the highest reported
   score" when the papers start from different base models; a rank is fair only when the comparison fixes the base
   model and the setting. When you say a result has no spread, check its record: a paper can give a spread in one table
   (mean ± std over repeated runs) and one value in another, and repeated runs with one seed are not a spread across
   training seeds.
5. **`gaps`** (section 9, 3-8 items). Give each gap one `type`:
   - `method-limitation`: a demonstrated failure or a restrictive assumption of a method.
   - `evidence-gap`: a relevant condition that the papers did not test.
   - `engineering-gap`: an approach needs much work before a team can use it (no code found, no weights).
   - `research-opportunity`: a plausible direction to improve understanding or performance.
   Use at least two types. Write a gap inside the review's coverage: "The reviewed studies do not establish ...", never
   "no work exists" or "has never been studied" (a selective review cannot see all work). Cite the problems, weak
   points, papers or open problems it rests on (`L`, `P`, `C`, `TK`, `OP` ids).
6. **`roadmap`** (section 10). Everything here is proposed, not conducted.
   - `baseline`: one included paper to establish first, and why (it is accessible: see `feasibility`; it is the
     reference that the others are measured against). The first experiment also measures the ordinary baselines a team
     would try anyway: the general base model as it is, plain supervised fine-tuning, and a smaller general model.
     "Accessible" means the authors' weights can be evaluated (`feasibility` "evaluate now"); retraining needs code.
   - `shortlist`: 1-4 included papers with the reason each is on the list.
   - `experiments`: 2-5 proposed experiments. Each has `experiment` (what to run), `question` (the uncertainty it
     resolves), `decision` (the decision it supports), `needs` (the data and the implementation it needs) and
     `criteria` (the result that continues, changes direction or stops the work). Use the spec's patterns where they
     fit: evaluate an accessible baseline on representative target data; compare two families under matched
     conditions; add one component to the baseline; evaluate predefined difficult subsets. Cite the records each one
     rests on.

Write ONLY `<review>/data/report/report.json`:

```
{"executive_summary": {
   "problem": {"text": "...", "refs": ["TK1"]}, "uncertainty": {"text": "...", "refs": ["L2"]},
   "next_step": {"text": "...", "refs": []}},
 "verdict": [{"goal": "...", "choice": "...", "cluster_ids": ["C001"], "principle": "...", "condition": "...",
              "support": "consistent | few | conflicting | untested", "refs": ["K001", "TK2"]}],
 "requirements": {"application_goal": "...", "formal_task": "...", "inputs_outputs": "...",
   "operating_conditions": "...", "constraints": "...", "research_questions": ["...", "..."]},
 "unmeasured": [{"text": "...", "refs": ["M003", "L4"]}],
 "conditions": [{"kind": "works", "text": "...", "refs": ["K001", "P012"]},
                {"kind": "unresolved", "text": "...", "refs": ["R0345"]}],
 "gaps": [{"type": "evidence-gap", "text": "The reviewed studies do not establish ...", "refs": ["L1"]}],
 "roadmap": {"baseline": {"paper_id": "P012", "reason": "..."},
   "shortlist": [{"paper_id": "P031", "reason": "..."}],
   "experiments": [{"experiment": "...", "question": "...", "decision": "...", "needs": "...", "criteria": "...",
                    "refs": ["P012", "L2"]}]}}
```

## Rules

- Ids exactly as the packet writes them. `merge-report` drops a ref that is not a record of the review, refuses an
  `unmeasured`, `conditions` or `verdict` item with no ref, a verdict row without its four texts or with an unknown
  family or support, a roadmap paper that is not included, and a gap type outside the four. It warns on a family in no
  verdict row and on a verdict text longer than its cell holds.
- Every number you write must be in the packet (counts up to 12 pass). Numbers from the KPI boards keep their units.
  Every count of papers, members or families comes from `counts`: never count the lists yourself.
- Claims about code or weights come only from `feasibility`: "not found" is not "none"; say "no release found". Keep
  evaluating (the authors' weights, with a standard loader or their code) apart from retraining (their code): a model
  whose weights are on Hugging Face can be evaluated without being rebuilt, even when no code was found.
- Call each family's best-ranked paper with a venue its "representative paper", never its "best paper".
- Keep a study's reported finding, your reading across studies and a proposed action apart: "reported" for a paper's
  result, "the review reads" or "the evidence suggests" for your synthesis, "propose" for the roadmap.
- Before you finish, run `python3 <litrev> check-json <your file>` and fix any error it reports.
- Say each thing once. A verdict row, a condition, a gap and an experiment each make their own point: the video
  narrates them in different chapters, and a point written twice is said twice.
- Return one line: the number of items in each list and any part you wrote as "not stated in the review".
