# Review schema (the contract)

All tables live as JSON under `<review>/data/` and are the single source of truth. Markdown, CSV,
xlsx and `hub.html` are rendered from them and never hand-edited. Quotes and captions in `data/` keep the source's TeX
verbatim; the rendered files show it as Unicode text (outputs.md, "Formulas"). `python3 scripts/litrev.py
validate` enforces everything marked REQUIRED.

## Contents
- IDs
- Evidence notes (statuses)
- Availability vocabularies
- Tables: search_terms, search_runs, discovery_links, papers, tasks, datasets, benchmarks, metrics, results
- Importing records (`import --file`)
- Step-5 detail file written by subagents
- Step 6: KPIs, clusters, explainers, figures
- Step 8: presentation (opt-in)
- Derived fields

## IDs

Sequential, zero-padded, never renumbered or reused: `Q001` search term, `S001` search run, `P001`
paper, `T001` task, `D001` dataset, `B001` benchmark, `M001` metric, `R001` result,
`C001` method cluster, `X001` explainer, `K001` KPI (in `meta.json`). Counters live in
`meta.json`. Papers additionally carry `ids: {arxiv, doi, openalex, s2}` used for de-duplication;
`upsert_paper` merges on any matching id or on an exact normalised title. Lists of linked IDs are
allowed everywhere (`task_ids`, `dataset_ids`, …).

## Evidence notes

Every substantive fact is an object, not a bare value:

```json
{"value": <any|null>, "status": "<status>", "source": "<url|cached-text-path|null>",
 "checked": "YYYY-MM-DD", "location": "Table 3 / Sec 4.2 / README", "quote": "<verbatim>", "rationale": "<why>"}
```

| status | meaning | value | source |
|---|---|---|---|
| `verified` | confirmed in an official/independent source (leaderboard page, repo fetched 200, HF API, OpenAlex record) | required | required |
| `author-reported` | stated in the paper itself; `quote` verbatim from the cached full text | required | required |
| `inferred` | not stated; derived by reasoning. `rationale` REQUIRED | required | required |
| `not-reported` | the full text was read and does not state it | null | — |
| `not-found` | searched the listed sources, found nothing. **Not a verified "no".** | null | — |
| `inaccessible` | could not read the source (no full text, paywall, dead link) | null | — |
| `not-applicable` | the field does not apply to this entity | null | — |

Rules enforced by `validate`: positive statuses need a non-null value and a source; `inferred` needs
a rationale; negative statuses need a null value. `merge-details` downgrades any author-reported
note whose `quote` is not literally in the cached text to `not-found` with a rationale, and turns
`not-reported` into `inaccessible` when only the abstract was available (abstract silence is not
"not reported").

## Availability vocabularies

- Code / checkpoint `value`: `{"availability": "yes|no|unknown", "url", "kind": "official|third-party|unknown", "notes"}`.
  `availability: "no"` is only legal with a positive status (someone verified the absence). Not found → status
  `not-found` with value null (readers show it as unknown); a value of `{"availability": "unknown"}` is not written.
- Dataset `availability.value`: `{"availability": "public|registration|request-only|restricted|unavailable|unknown", "url", "license", "notes"}`.
- Dataset / benchmark `leaderboard.value`: `{"exists": "yes|no|unknown", "url", "track", "active": "yes|no|unknown", "checked": "YYYY-MM-DD"}`.
- Paper `taxonomy_source: true` marks a survey used in step 0: typed `survey`, included, exempt from the cap.
- Paper `seed: true` marks the user's seed paper (set by `select` when its arXiv id or exact title is in `meta.seed`, or by hand): included, exempt from the year window and both caps.
- Paper `paper_role`: `method|evaluation|benchmark|survey` (absent = `method`). Anything but `method` REQUIRES
  `paper_role_reason` (whose methods it evaluates, what it benchmarks). Only `method` papers count towards KPI coverage
  and are ranked; the others are listed under `rank-papers` → `unranked` as "<role> study (not ranked): <reason>".
- Paper `status`: `included|candidate|excluded`; `excluded` REQUIRES `status_reason`. `manual: true`
  marks a decision recorded with `screen`; `select` never overrides it.
- Dataset / metric `status`: `included|candidate|excluded`; `candidate` = created from paper text and
  not yet described (warning); `excluded` REQUIRES `status_reason` and skips the REQUIRED checks.
- Search term `status`: `pending|searched|deferred|redundant|out-of-scope` (+ `status_reason` for the
  last three; `deferred` = not run this pass).
- Discovery `discovery_type`: `keyword|reference|citing|survey|dataset|leaderboard|seed`.
- Term `category`: `concept|task|method|dataset|benchmark|metric|application`.

## Importing records

`import --table <t> --file <records.json>` takes a JSON list (or one object). Each record updates the row with its
`id`, else the row whose name or alias matches its `name` exactly (case, punctuation and spaces ignored), else
creates a row. Every field is copied as given; nothing is checked at import time. `validate` checks the fields
later, against the table sections below. Write the file under `<review>/cache/app/scratch/` in app mode
(anywhere outside `data/` in skill mode), and check it parses with `check-json <file>` first.

Step 0 merges the survey taxonomies with one file per table. These records carry everything `validate` needs at
that stage:

```json
[{"name": "Monocular metric depth estimation", "aliases": ["metric MDE"],
  "definition": {"value": "Predict absolute depth in metres from one image", "status": "author-reported",
                 "source": "<review>/cache/fulltext/P002.txt", "checked": "2026-10-11", "location": "Sec. 2",
                 "quote": "<verbatim sentence from the survey>"},
  "inputs": "one RGB image (+ intrinsics if known)", "outputs": "per-pixel depth in metres", "parent_id": null}]
```

- **tasks:** `name` plus `definition` as an evidence note (above); `inputs`, `outputs`, `parent_id`, `aliases`.
- **datasets:** taxonomy datasets go in as candidates, `{"name": "KITTI", "aliases": ["KITTI Eigen"],
  "status": "candidate", "task_ids": ["T001"]}`. Candidates need no evidence notes; step 3 fills them (or excludes
  the dataset with a `status_reason`).
- **benchmarks:** `name`, `dataset_ids`, `split`, `protocol` (free text); `leaderboard` evidence comes in step 3.
- **metrics:** `name`, `aliases`, `definition` (evidence note), `higher_is_better` (`true`/`false`; a KPI needs it),
  `units_range`, `kind` (`published` or `proposed-kpi`).
- **search_terms:** `{"term": "zero-shot metric depth", "category": "concept|task|method|dataset", "origin":
  "survey:P002", "rationale": "why this term"}`; a term that already exists is reused, not duplicated.

## Tables

### search_terms (Q)
`id, term, aliases[], category, origin` (entity id, `seed`, or `survey:P00x`), `rationale,
related_ids[], run_ids[]*, discovered_paper_ids[]*, status, status_reason`.

### search_runs (S)
`id, kind` (keyword/reference/citing/survey/dataset/leaderboard), `query` (exact query string or
traversal description), `term_ids[], source` (arxiv/openalex/semanticscholar/hf/manual), `query_url,
executed_on, filters{}` (a `snowball` walk stores `{"hop": n}`, and its `refresh` replays keep it; seed/survey walks are hop 1; a reference walk parsed from the cached bibliography stores `{"from": "fulltext", "since_year": y}`)`, seed_paper_id, direction` (backward/forward), `scope` ("top 25 by
relevance"), `retrieved, screened, limitations`.

### discovery_links (list)
`run_id, paper_id, discovery_type, origin, decision` (candidate/included/excluded, mirrored from the
paper after `select`), `reason`. One row per discovery path; a paper found three ways has three rows.

### papers (P)
Identity: `id, ids{}, title, authors[], year, venue, type` (preprint/original/survey/…), `version, url,
abstract, categories[], is_survey, seminal, paper_role, paper_role_reason`.
Screening: `status, status_reason, discovery_run_ids[]*, sources[]`.
Links: `task_ids[], dataset_ids[], benchmark_ids[], metric_ids[], result_ids[]*`.
REQUIRED evidence notes for every **included** paper (value or explicit status):
`labs` (list of affiliations), `relevance`, `contribution`, `code`, `checkpoint`, `data`,
`representation`, `loss`, `topology`, `training_scheme`. Plus `citations` (evidence) and
`evidence_limitations` (free text: support quality, missing details, assumptions).
`published_venue` (evidence, set by `paper`/`search`/walks, never by hand unless sourced): value
`{name, short, year, kind}` (`short` e.g. `CVPR`, `kind` journal/conference/proceedings/workshop/book, or null when an arXiv comment names no kind). `verified` = an
OpenAlex published-version location in a journal/conference/proceedings source (title matches must share an author;
arXiv, repositories and preprint servers never count); `author-reported` = the arXiv `journal_ref` or an acceptance phrase /
"CVPR 2024" in the arXiv `comment` ("submitted to", "under review", "extension of" never count); `not-found` (value null)
= resolved but no venue found, which is **not** a verified preprint (OpenAlex lacks many CVF papers). `venue` stays the raw
first-seen string. `journal_ref` and `comment` keep the arXiv fields.

Field meanings for the five method-detail notes:
- `data` — pre-training/training/evaluation sources, modality, preparation, supervision type.
- `representation` — internal/output representation, granularity, spatial/temporal structure.
- `loss` — objective terms, their purpose, reported weights/schedules.
- `topology` — backbone, modules, connections, heads, frozen vs trainable parts.
- `training_scheme` — stages, supervision, sampling, optimisation, compute if reported.

### tasks (T)
`id, name, aliases[], definition` (evidence), `inputs, outputs` (ground truth), `relevance`
(evidence), `parent_id, related_ids[], paper_ids[]*, dataset_ids[], benchmark_ids[], metric_ids[], sources[]`.

### datasets (D)
`id, name, version, aliases[], status` (included/candidate), `task_ids[]`,
REQUIRED evidence: `ground_truth` (annotation types, granularity, coverage), `size` (counts **with
units**; keep frames / sequences / subjects separate; never sum incompatible units; say which splits
have hidden labels), `availability`, `real_synthetic` (`real|synthetic|mixed|unknown` + proportions),
`leaderboard`. Also `modality` (evidence), `paper_ids[]*, benchmark_ids[], metric_ids[]*, sources[], limitations`.

### benchmarks (B)
`id, name, version, aliases[], task_ids[], dataset_ids[], split, protocol, challenge{name, year,
track}, constraints` (input/supervision rules), `metric_ids[], eval_server, leaderboard` (evidence),
`docs_url, paper_ids[]*, sources[]`.

### metrics (M)
`id, name, aliases[], status, definition` (evidence; mathematical or operational), `units_range,
higher_is_better, aggregation, thresholds, task_ids[], benchmark_ids[], implementation_url,
limitations, kind` (`published|proposed-kpi`), `paper_ids[]*, sources[]`. Materially different
variants (e.g. δ1 at 1.25 vs 1.25²) get distinct IDs.

### results (R)
`id, paper_id, method_variant, checkpoint, benchmark_id|dataset_id, task_id, dataset_version, split,
split_note, track_year, protocol, metric_id, metric_variant, value, units, rank, rank_denominator,
snapshot_date, provenance` (`paper-reported|leaderboard-verified`), `row_kind, training_data,
test_time_adaptations, compute, source, location, quote, caveats, status`.
`row_kind` (v1.36.0): `own|ablation|baseline|oracle`, set by step 5. Only `own` rows rank a paper or count on a KPI
board. Rows without it (older reviews) are read from their text: caveat `baseline re-reported by` = baseline;
`oracle`, `upper bound`, `human performance` in the method name or caveat = oracle; `ablation` or `w/o` = ablation.
`split` is the split's name (`test`, `dev`, `small-test`, `subset`, `model-level`, `not stated`); sizes and sampling
go in `split_note`. `check-details` lists rows that break either rule.
One row per metric × setting × variant; ablations are separate rows; never copy the best variant's
score to other variants. `leaderboard-verified` REQUIRES `snapshot_date`; a `rank` REQUIRES
`rank_denominator`. Leaderboard verification is not independent reproduction. The renderer adds
`rank_in_reviewed_set` (computed across the included papers per dataset×metric×split) as a separate
column; it never overwrites `rank`.

## Step-5 detail file (written by one subagent per paper)

Path: `<review>/data/details/P00x.json`

```json
{"paper_id": "P00x", "extracted_at": "2026-10-02T12:00:00Z",
 "data": {"value": "…", "status": "author-reported", "quote": "<verbatim from the cached text>", "location": "Sec 3.1"},
 "representation": {...}, "loss": {...}, "topology": {...}, "training_scheme": {...},
 "evidence_limitations": "…",
 "datasets_used": [{"name": "KITTI", "role": "train|eval", "quote": "…"}],
 "results": [{"dataset": "KITTI", "metric": "AbsRel", "split": "test", "method_variant": "ViT-L", "value": 0.058,
              "units": null, "protocol": "zero-shot", "quote": "…", "location": "Table 2", "caveats": null}]}
```

Not stated → `{"value": null, "status": "not-reported"}`. `merge-details` sets `source`/`checked`,
checks every quote against the cached text, canonicalises dataset/metric names via `aliases`, and
creates `candidate` datasets/metrics for unknown names (with a warning). A name matches a record only when it equals
the record's name or an alias after normalising case, punctuation and spaces, or when its parenthetical-stripped form
does ("KITTI (Eigen split)" → "KITTI"); the parenthetical alone never matches ("TREC DL 2019+2020 (MS MARCO passage)"
is not "MS MARCO passage"). `fold` does not add a folded name as an alias when it is a generic part of the target's
name ("TREC DL" into "TREC DL 2019+2020 (pooled)") unless `--keep-alias`. Every included non-survey paper is expected to
have a details file; `details-status` and `merge-details` (`missing_details`) list the ones that do not.

## Step 6: KPIs, clusters, explainers, figures

**KPIs** live in `meta.json` as `kpis: [{id: "K001", benchmark_id|dataset_id, metric_id, metric_variant|null, splits: [label|null], rationale}]`
(`metric_variant` null = rows of any variant; set it to rank one threshold)
plus `top_n` (default 10). Max 3; each covers ≥3 distinct included non-survey `method` papers; the metric has
`higher_is_better` set. A paper's ranking (`rank-papers`) is derived: best rank across KPIs, credited to
the best variant; sort by best rank → KPIs covered → published venue before none → K001/K002/K003 rank → verified
citations → id. A venue only breaks ties; it never lifts a paper over a better KPI rank.

**papers.figures** — evidence note; value `[{label, url, images[], caption}]` from the cached arXiv HTML,
status `verified` with the HTML URL as source; `inaccessible` when the text did not come from arXiv HTML.

**Synthesis** lives in `meta.json` as `synthesis: {takeaways: [{text, detail?, rationale, cluster_ids[], evidence:
[{paper_id, quote, location}], emphasis[]?}], source, merged_at}`; every evidence quote is in that paper's cached text and
every takeaway keeps at least one. `detail` (v1.23.0, optional) is the 40–80 words behind the takeaway's
"More detail" fold: the numbers, ranks and papers behind it.

**Limitations** (v1.27.0) live in `meta.json` as `limitations: {problems: [{id, title, kind, text, detail?, emphasis[]?,
cells: {C00x: {state: affected|addresses|not-shown, basis?: author-reported|inferred, paper_id?, quote?, location?,
rationale?}}}], open_problems: [{title, text, emphasis[]?, related[], basis: "outside-knowledge"}], basis: {winners,
members, missing_members[]}, source, merged_at}` (from `data/explainers/limitations.json`, merged by
`merge-limitations`). `kind` is a weak-point kind (or `other`). Every family has a cell. An `affected` or `addresses`
cell names a member of its family; an `author-reported` cell (every `addresses` cell) quotes that member's cached text;
an `inferred` cell has a `rationale` and an optional quote. A cell that fails a check becomes `not-shown`. Each problem
keeps at least two `affected` families (one when the review has one family). Open problems come from the writer's
general knowledge: nothing checks them, so `basis` is always `outside-knowledge` and the hub labels them.

**emphasis** — optional list on each long step-6 text: the explainer's four text fields, each declared limitation
and weak point, each takeaway, each limitations problem and open problem; and on the five method notes (`data, representation, loss, topology,
training_scheme`) of each explainer's paper, which the slide's "In this review" strip shows (`merge-emphasis` writes
them into `data/details/<P-id>.json` and the papers table; `merge-details` keeps them while the text is the same). 1–3 phrases (`EMPHASIS_MAX`), each ≤6 words (`EMPHASIS_WORDS`), copied verbatim from
that note's `value` (a takeaway's `text`) as whole words; the merges drop any other phrase. The hub highlights the
first whole-word occurrence of each, longest first, never overlapping, and shows only 1 in a text under 40 words
(`EMPH_SHORT_WORDS`), else 2; REVIEW.md bolds the same ones. Absent = the writer gave
none (`status` lists these; `emphasis-input` → subagent → `merge-emphasis` adds them); `[]` = nothing worth marking.

### clusters (C)
`id, name` (6–12 words that say what the methods do and how they differ; `merge-clusters` notes other lengths under
`style`), `definition` (evidence, `inferred`, source `data/clusters/clusters.json`), `distinguishing_trait,
survey_family, members[{paper_id, field, rationale, quote|null, location}], paper_ids[]*, winner_paper_id*,
winner_reason*`. Every current top-N paper is in exactly one cluster. The winner is the best-ranked member with a
published venue; only when no member has one is it the best-ranked member, and `winner_reason` says which rule applied and
names a higher-ranked member without a venue.

### explainers (X)
`id, cluster_id, paper_id` (= the cluster's winner), `headline` (≤12 words), `problem, key_idea,
how_it_works` (evidence, `author-reported`, quote from the winner's cached text), `vs_siblings` (evidence,
`inferred` with rationale; `not-applicable` for a single-paper cluster), `figure{label, reason}|null`
(label must exist in the winner's `figures`), `glossary[{term, plain}]`, `facets{supervision, prior, output}` (evidence, `author-reported`, ≤10-word values for the
synthesis table), `details{problem?, key_idea?, how_it_works?, vs_siblings?}` (v1.23.0: the longer text behind
each section's "More detail" fold, 40–80 words; evidence notes, `author-reported` with their own quote for the first
three, `inferred` for `vs_siblings`; a detail whose quote is not in the cached text becomes `not-found`; absent on
older explainers), `diagram{nodes[{id, label (≤6 words), kind input|step|output}], edges[[from, to]]}|null`
(v1.23.0: the method as 3–8 boxes, drawn at the top of the slide; `merge-explainers` drops one with an unknown id, a
box with no arrow or a loop). ≤120 words across the four core text
fields. All step-6 text follows `prompts/ste-style.md` (ASD-STE100, about 80%); the merges list texts that break
its checkable rules under `style` in their report (notes, not `validate` warnings). KPI line, code and checkpoint on the slide are read from the ranking and the paper at render time.
The winner's critique (from `data/explainers/C00x.critique.json`, merged by `merge-explainers`):
`declared_limitations[]` (evidence notes, `author-reported`, each quoting the winner's own admission; one
`not-reported` note when the paper states none) and `weak_points[]` (2–5 evidence notes, `inferred`, each with a
`rationale`, a `kind` from evaluation|comparison|generalisation|assumption|cost|reproducibility|data|claim, and an
optional verbatim `quote`; a quote not in the cached text is dropped, the point kept). Both are cleared when the
explainer is rewritten for a new winner. `validate` errors on an explainer with no critique.
`member_limitations` (v1.27.0): `{P-id: [evidence notes]}` for each non-winner member of the cluster, the limitations
its authors state (from `data/explainers/limits/<P-id>.json`, `prompts/member-limitations.md`), settled like
`declared_limitations`; rebuilt on every `merge-explainers` for the current members only.

**Scope and decisions** live in `meta.json`: `scope: {in[], out[], protocol[], notes, agreed_on, by}` (set by
`scope`, step 0) and `decisions: [{id: "DEC001", date, kind, decision, reason, refs[], by}]` (append-only, `decide`;
`kind` scope|cap|screen|protocol|kpi|other, `by` user|agent; `refs` must name existing records or KPIs; `scope` logs
one `scope` decision). `validate` errors on a decision without a reason and warns when a review created on or after
2026-10-08 has no scope.

**Screening** (step 1): a paper screened by `merge-screening` carries `screening: {verdict: on-topic|off-topic|needs-text,
kind: method|dataset|benchmark|survey|evaluation|other, scope_ref, reason, area, source, batch_written, at}`
(`batch_written`: the batch file's mtime, so a re-merge skips batches no newer than the one a verdict came from); `scope_ref` is a scope
`in`/`out` item verbatim or `"matches no in item"` (then `area` names the research area). Off-topic papers get
`status_reason` `screening: <scope_ref>: <reason>`. `meta.boundaries: [{area, paper_ids[], decision_id|null}]` collects
the no-in areas across merges; one with >= 5 papers and no `decision_id` (set by `decide --boundary`) makes `validate`
warn. `validate` also warns about papers excluded for text access with no `fulltext` attempt, about cached `jina-oa`
texts that look like a paywall page, about included papers screened off-topic with no later `screen` decision, and,
once a review uses screening, about included papers that were never screened. `screen` stamps `manual_at` (ISO, UTC);
`merge-screening` never overrides a manual decision newer than the batch, or one without `manual_at`.

**Adjacent topics** live in `meta.json` as `adjacent: {generated_at, source, topics: [{id: "A01", name, definition,
relation, scope_out|null, decision_refs[], paper_ids[]}], in_scope[], unrelated[]}` (written by `merge-adjacent` from
`data/adjacent/topics.json`; regenerate rather than edit). Every excluded paper sits in one topic, in `in_scope`
(fits the scope, excluded for text access, window or cap) or in `unrelated`. Only excluded papers are accepted;
`validate` warns about excluded papers in none of them and grouped papers that are no longer excluded.

**Coverage** (`data/coverage.json`, derived by `coverage`, regenerate rather than edit): `generated_at,
included_count, window_from, min_links, neighbours[], neighbours_total, survey_refs[{survey_id, title, total, refs[]}],
gaps{zero_ref_walks[], skipped_terms[]}, failed[{paper_id, direction, status}]`. A neighbour or survey ref is
`{title, year, citations, s2, arxiv, doi, pool: {paper_id, status, reason, manual} | null}`, neighbours add `links`
and `via[{paper_id, direction: cited-by|cites}]`. Only works in the year window count. Adding the findings writes one
search run of kind `coverage` (source semanticscholar) with `reference` / `citing` / `survey` discovery links.

## Step 8: presentation (opt-in)

**Opt-in** (`meta.presentation`, written by `presentation`): `{wanted, minutes, audience, by: user|agent, at}`. Step 8
and its `status` rows exist only while `wanted` is true; `validate` never asks for it.

**Writer files** (inputs to the merges, one writer each): `data/presentation/plan.json` (the chapter plan:
`outcome, organizing_axis, axis_reason, headline_answer[], recurring_example{text, illustrative, refs},
chapters[{id CH##, title, question, functions[1-8], start_s, end_s, prior_knowledge, explanation,
evidence{supporting[], challenging[]}, comparability, cluster_ids[], deep_dive{paper_id, reason}|null,
visual{kind, refs[], message}, visual_sequence, example, engagement{kind, prompt}|null, takeaway, transition}],
folded_clusters[{cluster_id, into, reason}], companion[], missing_information[]`) and
`data/presentation/script/CH##.json` (`chapter_id, scenes[{id CH##-S##, narration[sentences], visual{kind, refs[],
reveal[{at, show[]}]}, on_screen, claim_type reported|synthesis|action|illustrative, sources[], pause_after_s,
transition cut|fade|none, engagement}], pronunciations[{term, say}]`). Packets (`*.input.json`) sit beside them.

**Merged** (`data/presentation/merged.json`, written only by `merge-presentation-plan` and `merge-script`; never edit
it): `plan` (the checked plan plus `minutes, target_s, wpm, source, merged_at`), `script{CH##: {scenes[] (each with
`start_s` and `est_s`, estimated from words at `wpm`), words, est_s, duration_s, source, merged_at}}` and
`lexicon{term: spoken form}`. `data/presentation/lexicon.json` (`{term: spoken form}`, written by the orchestrator, not
a writer) holds settled respellings that win over every writer's entry.

**Audio** (`data/presentation/audio.json`, written only by `audio`): `voice, engine{name, version, voice, speed},
asr_model, built_at, scenes{CH##-S##: {chapter, wav (cache/presentation/audio/<scene>.wav), dur, speech_end,
sentence_starts[], words[{text, start, end, sentence}] (the voice engine's word times, seconds in the scene), keys[]
(one hash per spoken sentence: voice + text after the lexicon), gaps[], loudness_lufs, asr{wer, missing, extra,
end_heard, char_sim, heard}}}, chapters{CH##: {dur, planned, drift}}, total_s, target_s, flags[]`. Sentence WAVs and
their index sit in `cache/presentation/audio/` (gitignored), keyed by the same hashes.

**Video** (`data/presentation/video.json`, written only by `video`): `mp4, srt, vtt, stills, segments, frames_expected,
probe{video_s, audio_s, frames, subtitles, size_mb}, narration_s, loudness, cues, render_s{stills, encode},
picture_changes{expected, found_in_mp4, missing[], max_offset_frames}, layout_warnings{"<scene> state k": [..]},
sync{events, voice_clock{checked, min_lead_s, max_lead_s, in_window}, recogniser{matched, median_s,
drift_first_to_last_third_s, outliers_over_0.5s[], unmatched[]}}, checks{frames_match, audio_matches_video,
picture_changes_found, no_layout_warnings, pictures_lead_speech, no_drift}, audio_built_at, built_at,
timeline[{scene, state, start_f, n_f, kind still|fade, from_scene?, from_state?}]`. Stills and segments sit in
`cache/presentation/video/`.

**Refs** a plan or scene may cite: record ids (`P C R T D B M` + digits), KPI ids (`K001`), decisions (`DEC001`),
limitation problems (`L1`), synthesis takeaways (`TK1` = the first in `meta.synthesis`), open problems (`OP1`) and
adjacent topics (`A01`). **Visual kinds** are a fixed set, each drawn from review data so that no number on screen is
a writer's: `title, chapter, claim, scope, evidence-flow, taxonomy, diagram (one C), figure (one P with figures),
results-table (one C), kpi-chart (one K), family-table, matrix, compare (two C), takeaways, open-problems, example
(illustrative)`.

## Derived fields (`*`)

Recomputed by `derive()` (called by every command): `search_terms.run_ids`,
`search_terms.discovered_paper_ids`, `search_terms.status` pending→searched, `papers.discovery_run_ids`,
`papers.result_ids`, `*.paper_ids` on tasks/datasets/benchmarks/metrics, `datasets.metric_ids` from
results, `discovery_links.decision` mirrored from the paper status, and `clusters.paper_ids`,
`clusters.winner_paper_id`, `clusters.winner_reason` from cluster members and the KPI ranking. Never set them by hand.
