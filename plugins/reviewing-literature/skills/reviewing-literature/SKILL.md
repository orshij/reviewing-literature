---
name: reviewing-literature
description: Use when the user asks for a literature review, a survey or map of a research area, "what papers / benchmarks / datasets / metrics exist for X", a related-work table, SOTA or leaderboard comparison, or wants methods, data, losses and training schemes compared across papers around a seed paper or concept; also when an existing review under the reviews folder (`$LITREV_HOME/reviews/`, default `~/code/literatureReview/reviews/`) should be extended, corrected, re-rendered or republished, or refreshed / checked for new papers; also when a review should become a narrated video, a talk or a presentation (chapter plan and narration script, step 8).
---

# Reviewing literature

## Overview

Build a literature review as **linked tables with stable IDs and per-cell evidence**, not prose.
`scripts/litrev.py` owns the store, the fetchers, validation and rendering; you own the judgment
(taxonomy from surveys, relevance, reading papers). Every substantive cell is an evidence note with
an explicit status, and `validate` refuses tables that lack them. Not found is never a verified no.

**REQUIRED reading before step 0:** `schema.md` (the table contract) and `sources.md` (what each
API can and cannot do). Read `outputs.md` at step 7. Subagents read their own instructions from
`prompts/`. All of these sit next to this SKILL.md; read them whole.

To extend or fix an existing review, run `status` first (it lists steps that are todo or stale since a later
command, and the next command), then start at the step that owns the change (a new paper: step 1
`paper`/`screen`, then steps 4–5 for it; a wrong cell: `set`; a display change: step 7) and finish with
step 7 so `validate --strict` passes and the hub is republished to its stored `hub_url`.

**Refresh an existing review (alert only).** `refresh --review <dir> --dry-run` shows the replays (each searched term
and citing walk, restricted to papers newer than its last run) and the request count; `refresh --review <dir>` runs
them, stores new papers as unscreened `candidate`s and writes `rendered/REFRESH.md`. It never selects, ranks or
publishes. Read the report to the user with a one-line recommendation per candidate (papers found by several paths
come first and are the likeliest fits); `screen` in only what the user approves, then run steps 4–7 for those papers.

Run every command as `python3 ${CLAUDE_SKILL_DIR}/scripts/litrev.py <cmd> --review <path>`
(write the full path; in zsh a `$L` variable holding `python3 …` does not split into words).
Reviews live in `<home>/reviews/<slug>/`, where `<home>` is `$LITREV_HOME` if set, else
`~/code/literatureReview` (one folder per review; its `cache/` is
gitignored). Default caps: 128 papers, last 3 years, plus a seminal exemption (≥1000 citations, max 10) that is
**additive** to the cap; pass `--max-seminal 0` at init if the user's number is a hard total. The exemption is only
for papers older than the year window: a highly cited paper inside the window competes for the cap like any other
(before v1.33 it went to the seminal pool and was cut as "over seminal cap"; SparseGPT, AWQ and Wanda were lost so).
Set `LITREV_MAILTO` to the user's email for OpenAlex's polite pool.
**One writing command per review at a time.** Every command that changes data holds an exclusive lock on the review
(`cache/litrev.lock`) for its whole run; a second one fails at once with "locked by … (enrich pid …)". Wait for the
first to finish, then rerun it. Read-only commands (`show`, `list`, `validate`, `details-status`, `check-details`,
`kpi --suggest/--list`, `rank-papers` without `--top`, `select --preview`) never wait, so run those freely while a
background `enrich` or `fulltext` is going.

## Checklist (copy into your todo list; one item per step)

```
- [ ] 0 init + scope agreement (ask the user; `scope`) + surveys → seed_taxonomy
- [ ] 1 search terms → runs → candidates → screen (subagents) → select → snowball + screen + select until `converged` → coverage + screen + select
- [ ] 2 tasks table; task names become new terms; search + select again
- [ ] 3 datasets + benchmarks + metrics (subagent per dataset)
- [ ] 4 paper · fulltext · enrich on the included set
- [ ] 5 details fan-out (subagent per paper) → details-status → merge-details
- [ ] 6 paper_role for evaluation/benchmark papers → kpi → rank-papers → figures → clusters (1 subagent) → merge-clusters → explainers + winner critiques (2 subagents per cluster) + member limitations (1 subagent per non-winner member) → merge-explainers → synthesis (1 subagent) → merge-synthesis → limitations (1 subagent) → merge-limitations
- [ ] 7 adjacent topics (1 subagent) → merge-adjacent → review report (1 subagent) → merge-report → validate --strict → render → publish hub → PROJECT.md → commit
- [ ] 8 only if the person opted in (`presentation`): presentation-input → plan (1 subagent) → merge-presentation-plan → script-input per chapter → scripts (1 subagent per chapter) → merge-script → audio --sample → audio → video → render → republish
```

## Step 0 — init, scope, surveys

1. `init --path <home>/reviews/<slug> --slug <slug> --purpose "<why the user wants this>" --seed "<concept or paper>" --categories <cs.CV,...>`.
   Categories come from the seed paper's arXiv categories (`paper <arxiv-id>` then `show`) or from
   the first survey hit; never a hardcoded default.
2. **Agree the scope with the user before searching**, in one message: what is in, what is out (neighbouring
   families you will see and leave out, e.g. "multi-view point-map methods"), and protocol rules for comparing
   results (e.g. "zero-shot only"). Record it: `scope --in "..." --out "..." --protocol "..." --notes "<the user's words>"`.
   Add one sentence to the same message: "Do you also want a narrated 20-minute video of the review (chapter plan,
   narration script, voice and MP4)? It costs extra tokens: about USD 18–34 of usage (measured: a 20-minute video
   used 2.2 M subagent tokens, its repeat and timing fixes included); the voice and the video render on this Mac and
   cost none." The length is 20 minutes (the user, 2026-10-28) unless
   the person names another (3–60); do not ask for it. Record a yes with `presentation [--minutes N] [--audience ".."]`
   and a no with `presentation --off`; without an answer, record nothing (step 8 then stays out of `status`).
   This is the run's only planned question; later judgement calls (a cap change, a protocol exclusion, keeping a paper
   against the rule) go in `decide --decision "..." --reason "..." --kind cap|scope|screen|protocol|kpi|other
   [--refs P012,B001] [--by agent]`, and both show on the hub's Decisions page. `validate` warns on a new review
   with no scope. The MDE review changed scope mid-run (monocular only, cap 12 → 18 → 30) after DUSt3R/VGGT surfaced.
   **When the subject is an intersection** ("X methods for domain Y"), put the target Y into every method item and
   name the general X as an `out` item ("general-purpose X with no domain or task target"); never one item that admits
   "general or domain-aware X". `select` ranks by citations, so general X let in by the same item fills the cap:
   llm-to-domain-slm scope v1 gave 46 of 137 slots to general compression methods, while D-Pruner and MixCal, found
   and screened on-topic, were cut at ranks 214 and 323 (the user: no interesting domain-compression paper).
   `validate` warns on such an item. Silence is not agreement: when the person answers the scope message without
   addressing the scope, say in one line which reading you took and record it `--by agent`, not `--by user`.
3. Find 2–4 surveys: `search --term "<concept> survey" --category concept --origin seed --rationale "survey discovery" --limit 15`
   (also try `review`, `overview`). Mark survey candidates with `set --table papers --id P00x --field is_survey --json true`
   and `paper P00x` to get citation counts; keep the 2–4 most cited and recent, and mark each one you
   actually use with `set --table papers --id P00x --field taxonomy_source --json true` (typed `survey`,
   kept in the table, exempt from the paper cap).
4. `fulltext P00x` for each survey, then **one subagent per survey** (`prompts/survey-taxonomy.md`) writes `data/seed_taxonomy/P00x.json`: tasks, datasets, benchmarks, metrics, method
   families, 8–15 candidate search terms, each with a verbatim quote and section. Merge them
   yourself into `import --table tasks|datasets|benchmarks|metrics|search_terms --file ...` with
   `origin: "survey:P00x"` on terms. Without subagents, write the taxonomy file yourself: new files
   under `data/seed_taxonomy/`, `data/details/` and `data/datasets_step3/` are the intended inputs;
   the rule against hand edits covers the nine table files `data/<table>.json`.

## Step 1 — search terms → papers

- For every term with status `pending`: `search --term "<term>" --category <cat> --origin <Qxxx|survey:P00x|seed> --rationale "<why>" --limit 25`.
  arXiv is the default engine (word-AND query, category and date window applied). Use
  `--engine openalex` only for a term the arXiv route misses (10 credits each; filtered to
  articles/preprints in the year window and ranked by relevance, but it has no arXiv-category filter).
- Citation walks from the seed(s) and surveys: `references P00x --limit 50` (fetches every reference and keeps the 50 most cited
  in the year window; pre-window classics only fill leftover slots, and only when `max_seminal` > 0) and `citations P00x`
  (keeps the 100 most-cited citers; `--limit` to change). Before v1.12.1 the reference walk kept the first 50 ids in
  OpenAlex order, which is oldest first; `validate` warns on such runs (dashcam S009 lost TransGeo and L2LTR to it).
  Walks from a survey are recorded as `discovery_type: survey`. **OpenAlex often has no `referenced_works`** for
  recent CVF-indexed and arXiv-only records (the 2025 depth-foundation survey had 0, and so did DUSt3R and VGGT);
  `references` then falls back to Semantic Scholar (`/references`, no key needed; run `filters.fallback:
  semanticscholar`, S2 citation counts), and if S2 has nothing too, to the bibliography of the paper's cached full
  text (`fulltext P00x` first; forced with `--from-fulltext`): each in-window entry is resolved free through arXiv,
  the run carries `filters.from: fulltext` and the unresolved entries in `limitations`, and `--no-search` skips the
  10-credit OpenAlex title search per entry. Every forward walk (`citations`, `snowball`) also adds S2's most-cited
  citing works that OpenAlex did not list (`filters.supplement`), since OpenAlex never lists a citing paper whose own
  record has no references (Depth Anything 3 was missing from every OpenAlex forward walk; the S2 supplement found it
  through 8 included papers). If S2 fails (HTTP 429), the walk keeps OpenAlex's list, warns and flags the run (`filters.s2_failed`); the next
  `snowball` re-walks flagged runs first and `status` lists the ones left (table-qa: 75 walks without a key). S2 counts run higher than OpenAlex's, so S2-found candidates get a mild `select` edge; `paper` on the ones you
  screen in replaces them with the OpenAlex published count. A reference walk that still finds 0 prints a WARNING
  and `validate` warns until it is rerun. The 2025 depth-foundation survey had 0 OpenAlex refs; S2 had 222, among
  them DUSt3R and VGGT, which the review had missed.
- `select` applies the caps and writes `status` + `status_reason` on every paper and mirrors the
  decision into `discovery_links`. The seed paper is exempt from the year window and both caps: `select`
  recognises it when its arXiv id or exact title is in `--seed`; otherwise mark it with
  `set --table papers --id P00x --field seed --json true`. Once a review screens, `select` fills the cap with papers
  screened on-topic first (by score), and only then with unscreened ones (cap-excluded before screening, or never
  screened); those are listed in `unscreened_included`: screen them (`screen-input --status included` → subagent →
  `merge-screening`) and `select` again. Then `paper included` to get published-record citations and
  institutions for the included set (this is the paid OpenAlex step; see budget rules in sources.md).
- **Screen every batch of new candidates per paper, against the scope, before `select`** (after each search pass,
  walk, `snowball` hop and `coverage`): `screen-input` writes `data/screening/input-NN.json` (150 unscreened candidates
  each, with abstracts), **one `litrev-screener` subagent per packet** (`prompts/screen-papers.md`, on Sonnet) writes
  `batch-NN.json` with one verdict per paper (`on-topic` citing a scope `in` item verbatim, `off-topic` citing an
  `out` item or "matches no in item" plus an `area`, or `needs-text`), and `merge-screening` applies them: off-topic
  is excluded with that paper's own reason, on-topic stays a candidate for `select`, and a verdict that cites no scope
  item, or excludes a dataset/benchmark/survey paper when the scope covers those, is rejected. Then `fulltext` the
  `needs_text` papers, `screen-input --needs-text --batch 30` → subagent → `merge-screening` again; a paper whose text
  still cannot be fetched is excluded for text access, never for topic (since v1.36.0 `merge-screening` does this by
  itself for needs-text candidates whose `fulltext` is abstract-only and whose record has no abstract, and
  `screen-input --needs-text` leaves them out of the packets). Re-merging is safe: a batch already applied to
  a paper (or older than the batch its verdict came from) is skipped, and a manual `screen` decision is kept unless the
  batch was written after it (`kept_manual`); `--batch 07[,08]` applies only those files. Included papers are screened
  too (`screen-input --status included`, which `validate` asks for when any were never screened): their verdict is
  recorded but never excludes them; `included_off_topic` papers go to the user, and their answer is recorded with
  `screen --decision included|excluded` (`included_needs_text` ones: `fulltext`, then `screen-input --needs-text`). `fulltext` rejects jina pages with a paywall marker or under 12k chars of
  prose (IEEE Xplore without a subscription); `validate` lists cached ones for `fulltext --force`. When the report lists `undecided_boundaries`
  (≥5 papers in one area no scope item names), ask the user and record it with
  `decide --kind scope --boundary "<area>" [--boundary "<same area, other name>"] --decision ... --reason ...` (and
  `scope` again if it widens). Parallel agents may name one area twice: merge the names into one question, and ask
  all open boundaries in one message.
  **Never exclude a batch by title with one shared reason**: the dashcam review excluded 255 papers in one call as
  "off-topic by title", which dropped a dataset paper the scope included, three open-access papers never fetched, and
  the whole OpenStreetMap area without asking. `screen` stays for single papers and the user's requests.
- Review `list --status excluded` and `list --status included`. `select` is relevance-blind (it
  ranks by citations, discovery paths (a `coverage` run counts as one path, however many included papers link to
  the paper), survey mentions, recency and a +1.5 bonus for a published venue; before `paper`, keyword hits have only their arXiv comment as
  a venue source while walk hits already carry OpenAlex's), so screen the result: an
  off-topic paper that scored in (a dataset paper, a different task) gets
  `screen --id P0xx --decision excluded --reason "..."`; a relevant paper that scored out is first
  chased by adding the term or citation walk that should have found it, then `select` again, and
  only then `screen ... --decision included`; a relevant paper that was *found* but outranked is
  screened in directly with a reason. Expect this: `select` sees preprint-record citation counts for
  arXiv hits and published counts for citation-walk hits, so an off-topic walk from a broad survey can
  outrank on-topic keyword hits. Manual decisions are kept on re-`select`. After the first `select`,
  read every included title: a generic vision or ML paper (a backbone, a feature descriptor, a
  general survey) that entered through the seminal exemption is screened out with that reason.
- Snowball (multi-hop forward walks), after that first screening; **required, not optional** (the dashcam review predates
  it and missed papers that cite five of its included papers): `snowball` walks `citations` from every
  included paper that a walk found and that has not been walked itself (hop 2 from the seed/survey walks, then
  hop 3, ...; the hop is stored in the run's `filters.hop`). Then screen the new candidates (`screen-input` →
  subagents → `merge-screening`, above) and `select`: `select` is relevance-blind, so without screening it lets in
  generic surveys and leaves out on-topic papers with few citations. Only included papers grow the next frontier, so a paper you screen out is never walked from.
  Repeat until `snowball` prints `converged` or hop 10 (`--max-hop`, default 10). `snowball` saves after each paper it
  walks, so a hop that a tool timeout stops keeps its walks: run `snowball` again and it walks only the rest of the hop.
  A spent OpenAlex daily budget does not stop a hop: the walks use Semantic Scholar (see sources.md, HTTP robustness). Retrieval-confidence found nothing new
  at hop 4 and converged on the next call (54, 3, 0 new papers; 9 screened in). Papers screened in this way need `paper`, `fulltext`, step 3 and step 5 like any other.
  Most walk-found papers are publisher records (ACM, Springer, IEEE) with no arXiv id: run `paper` and `fulltext` on them
  *before* screening them in. One still `abstract-only` after the open-access fallback gives step 5 nothing to extract,
  so screen it out with that reason unless the user wants it listed only (retrieval-confidence: 17 of 25 were).
  Save `list --status included` before every later `select`; if a paper you already enriched or
  extracted drops out, `screen` it back in or record why not. Screen in one pass, not one paper per
  `select` round: `select --preview 30` writes nothing and prints the would-be included set plus the next
  30 papers that would fill freed slots; an off-topic one there was missed by screening: fix it with `screen` and a
  reason of its own, then run `select`. Terms not run
  this pass get `status: deferred` + reason; useless ones `redundant|out-of-scope` + reason (via `set`).
  **Balance the families.** When the review compares method families that cite at very different rates, citation rank
  lets the most-cited family take the cap: count the included set by each paper's `screening.scope_ref` and, if a
  family the review compares is crowded out, reserve slots with `select --share K=N` (scope in-item K, 1-based, gets
  at least N slots from its best-ranked on-topic papers; already-included papers of the item count toward N; repeat
  per item; `N=0` removes; stored in `meta.caps.shares`, and `--preview` tries floors without storing). Take N from
  the item's on-topic pool after its searches and walks have run, never the pool size itself, and record it with
  `decide --kind cap`. Item numbers follow the scope's `in` order: after a new `scope`, check the stored shares.
  llm-to-domain-slm v2: compression got 6 of 132 slots by rank against 54 PEFT and 43 distillation papers.
- **Coverage, after snowball converges** (and again before publishing): `coverage` reads the Semantic Scholar
  references and citers (up to 3000) of every included paper and lists works linked to at least 2 included papers
  (`neighbours`, by links then citations), plus each walked survey's in-window bibliography (`survey_refs`, by
  citations); it reruns reference walks that found nothing and adds the findings as unscreened `candidate`s with one
  discovery link per linked included paper. Then screen the new candidates and `select`, as after `snowball`. It also prints the search terms never run: search each one, or record why not with `decide`.
  `--report-only` writes `data/coverage.json` and changes nothing; `--no-fetch` reads the cache only. A failed S2
  list (HTTP 429) is reported, and the hub says the list is partial. On MDE at 12 papers it put UniDepthV2,
  Pixel-Perfect Depth, ZoeDepth and MoGe-2 in its top 8 of 4,788 and DUSt3R/VGGT at #10/#11 of the survey's list,
  all missed then; on dashcam it found TransGeo (24 links) and L2LTR (22), never candidates. Generic backbones
  (DINOv2, latent diffusion) also rank high: screen them out with a reason.
  To answer "why isn't X in the review?": `why-missing "<title | arXiv id | P id>"` gives the verdict (in the review,
  in the pool with its status and discovery paths, a coverage neighbour, a survey reference, or never seen with the
  likely causes). The hub's Coverage page does the same lookup.
- Remove junk records (a wrong paper, a dataset that is only a training source) with
  `delete --table <t> --id <id>`; it drops every reference. Never edit `data/*.json` by hand.

## Step 2 — tasks

Build `tasks.json` from the survey taxonomy plus the included abstracts: canonical name, aliases,
definition (evidence with quote), inputs, outputs/ground truth, relevance to the user's purpose,
parent. `import --table tasks`. Set each included paper's `task_ids` (`set --table papers --id P00x --field task_ids --json '["T001"]'`).
Then add each task name as a `category: task` search term with `origin: T00x`; run as many as the
term budget allows and mark the rest `deferred` with a reason. `select` again; the cap holds and newly
included papers go through steps 4–5 too. Re-running an existing term text with another engine reuses
its Q id. `validate` cannot pass before step 5 (included papers lack their REQUIRED notes); use it here
only for FK and vocabulary errors.

## Step 3 — datasets, benchmarks, metrics

For each dataset/benchmark named in the taxonomy or the included abstracts, a subagent
(`prompts/dataset-record.md`; one dataset each, or 4–5 per agent when agents are limited) fills the
REQUIRED evidence and writes `data/datasets_step3/<slug>.json`; you `import --table datasets` each file. Datasets that turn out to be out of scope get `status: excluded` + `status_reason`. Benchmarks (dataset + split + protocol + challenge
track) are separate records in `benchmarks`. Metrics get definition, units/range, higher-is-better,
aggregation, thresholds and whether they are published metrics or KPIs you propose
(`kind: proposed-kpi`). Keep frames, sequences and participants as separate counts; never sum
incompatible units; say which test labels are hidden.

Datasets a paper built itself (its own recordings, a simulation, a re-split) get records too, from
the same prompt with `paper_ids` instead of a homepage. After `merge-details` (step 5), resolve every `candidate` in this order:
fold a variant or alias into its canonical record with `fold --table metrics|datasets|benchmarks --from M014
--into M010 [--variant-from-name] [--keep-alias]` (repoints every result row and link, keeps the old name as an alias so later
`enrich`/`merge-details` runs land on the canonical record, deletes the candidate; `--variant-from-name` puts the
folded metric's name in `metric_variant` of rows that have none). A folded name that is only a generic part of the
target's name ("TREC DL" into "TREC DL 2019+2020 (pooled)") is not kept as an alias, since every later paper saying
"TREC DL" would land on the pooled record; the output lists it under `aliases_skipped`, and `--keep-alias` forces it.
Name matching is exact after normalising case, punctuation and spaces ("MS-MARCO" = "MS MARCO"); a parenthetical is
only ever stripped ("KITTI (Eigen split)" → "KITTI"), never matched on its own. Exclude imagery
sources, pre-training sets and private data with a reason; `delete` a placeholder name such as
`not stated` (`merge-details` no longer creates them; older reviews may hold some); fill the rest with
a step-3 subagent.

## Step 4 — per-paper enrichment (deterministic)

`fulltext included` then `enrich included`. A paper with no arXiv id (a publisher record from a walk) first gets one
from an exact-title arXiv search (free; dashcam's TransGeo and SAIG were abstract-only without it; an id another paper
already holds is never taken). `fulltext` then tries arXiv HTML, then jina, then a local
`pdftotext` conversion of the arXiv PDF, then the open-access PDFs on the paper's OpenAlex record, looked up by DOI
when the record has no OpenAlex id (repository copies first, method `pdf-oa`; a PDF that Python's client gets as a
bot-check page, as nature.com serves it, is retried with curl), then jina on the OA landing pages (method `jina-oa`;
MDPI, IEEE and Wiley gold OA serve HTML, and a page that is a bot check or not a full paper is rejected), then
abstract-only. dl.acm.org and Wiley often refuse bots even for OA papers; dashcam's P448, P587 and P622 were
abstract-only before the DOI, curl and landing-page routes. Re-run `fulltext <P-ids>` for any paper
included after this pass; `validate` warns about included papers with no cached text. An arXiv text can be older than
the paper's journal version: Meerkat's arXiv v2 (2024) lacks the physician evaluation of its npj version (2025), and the
review said no evaluation used expert grades. The packets name such papers (`text_read`); for a paper the argument
rests on (a family's representative paper), `fulltext <P> --published` reads the open-access journal version and keeps
the cached text when there is none; then run step 5 again for it and `merge-details --replace <P>` (a plain merge only
adds rows, so the old text's rows would stay), rewrite its relevance and contribution notes, point its own KPI rows at
their benchmark and the KPI's split label again (Meerkat's new rows said "test" and fell off its board), and rerun its
family's slide and critique writers (their quotes are checked against the new text). `enrich`
fills `code` (HF paper page `githubRepo`, `pwc-archive` official-code flag, arXiv comment URL, then the repository
printed in the cached paper text that the text marks as the paper's own: "we release our code at", "Code:", a
first-page footnote, an owner matching an author or a repo named like the paper; a tool the paper only used, a LaTeX
template link or an ambiguous URL gives `not-found` with the skipped URLs in the rationale; last, a `pwc-archive`
non-official link only if the paper mentions it; fetches the URL and records its HTTP status),
`checkpoint` (HF models citing the arXiv id, classified official vs third-party; then Hugging Face models or a
collection the paper's own text gives as its release ("our models are publicly available at", a Code Availability
section; a base model's or a baseline's weights do not count, and each link must resolve on the HF API: Meerkat's model
card cites other papers' arXiv ids, so the filter never found it); then a weights link in the official repository
README, where a model that cites the paper counts as official (MMedLM's README links Henrychur/MMed-Llama-3-8B); the
authors' HF model also gets its `license`, parameter count `params` and loader `library` from its model card; **check
`official_models` for version mismatches**, e.g. a V2 checkpoint attributed to the V1 paper because
the org name matches, and fix with `set`), `labs` (OpenAlex institutions) and historical leaderboard
rows from `pwc-archive` as `leaderboard-verified` results with `snapshot_date 2025-09-13` (needs
`pwc-index` to have been run once on this machine; `enrich` warns if it is missing). Anything not found is recorded as `not-found` → availability
`unknown`. Fill `relevance` and `contribution` yourself for every included paper (evidence notes
with a quote from the cached text) via `set` or `import --table papers`.

## Step 5 — method details fan-out

One `litrev-details` subagent per included paper (`prompts/method-details.md`), 6 at a time, each reading only its
cached full text and writing `data/details/P00x.json`. Start the next paper as each one finishes. Agents can die on an API error, with or
without having written their file. **Before merging, run `details-status`**: it lists included non-survey papers with
no details file (`MISSING`), unparseable files (`INVALID`) and per-paper quote rejections, and exits 1 while anything
is missing or invalid; relaunch exactly those papers. `merge-details` also warns, on stderr and in its report's
`missing_details`, about any included paper still without a file. Then `merge-details`, which sets sources, drops any note whose quote is
not literally in the cached text (status becomes `not-found` with the rationale), turns
`not-reported` into `inaccessible` for abstract-only papers, canonicalises dataset/metric names, and
creates `candidate` datasets/metrics for unknown names. Resolve every warning with the candidate
recipe in step 3.

## Step 6 — rank, cluster, explain

Runs after `merge-details`. It answers: which included papers perform best on the review's headline
KPIs, which method families they fall into, and what each family's best paper does. Changing a
result, a KPI or the included set later means rerunning items 2, 4 and 5.

0. **Mark papers that are not methods before `kpi --add`.** An evaluation or reproduction study (it scores other
   people's predictors), a benchmark/dataset paper or a survey reports rows that belong to someone else's method.
   `set --table papers --id P0xx --field paper_role --json '"evaluation"'` (or `"benchmark"`, `"survey"`) plus
   `set … --field paper_role_reason --json '"reproduction study of published QPP predictors"'`. Such papers count towards no KPI
   coverage, are never ranked, and `rank-papers` lists them under `unranked` with the reason.
1. `kpi --suggest` lists every (benchmark or dataset, metric) board with its number of included
   papers per split label. Choose 1–6 KPIs that serve the user's purpose and cover the most papers.
   Step-5 files since v1.36.0 give `split` as the split's name only (sizes in `split_note`; `check-details` flags
   free text), so this is mostly a problem of older reviews.
   **The same split often appears under several labels** (`test`, none, `test (654)`, `Eigen test (652)`
   are all the 654-image NYUv2 test set): list every label of that one split with repeated `--split`
   (`--split none` = no label) and say why in the rationale. Never merge a different protocol (a
   validation subset, fine-tuned vs zero-shot). Example:
   `kpi --add --dataset D002 --metric M001 --split test --split none --split "test (654)" --rationale "…"`.
   **A metric whose rows carry thresholds in `metric_variant` (AUC@5/@10/@20, mean vs median) needs
   `--metric-variant "@5"`**, or every paper is credited its best threshold (`rank-papers` warns; `kpi --suggest`
   lists each board's `metric_variants`). `rank-papers` also warns when a board mixes scales (a percent row
   stored without a `%` in `units`); fix the row's `units` before ranking. Any units string containing `%`
   ("AUC (%)") is read as percent.
   A KPI needs ≥3 papers and a metric with `higher_is_better` set (`set --table metrics --id M00x --field higher_is_better --json false`).
   **Prefer a benchmark KPI (`--benchmark B00x`) over a dataset KPI** whenever the dataset's rows mix
   protocols (zero-shot vs fine-tuned, relative vs metric): a split label cannot separate them. Point
   each paper's own rows at the matching benchmark first, through the CLI: a file of
   `{"id": "R0xx", "benchmark_id": "B00x"}` records passed to `import --table results` changes only that
   field. Never move a baseline row, an ablation or an oracle. Rows whose caveat says
   `baseline re-reported by …` never rank their paper.
2. `rank-papers --top 10` prints each paper's best rank across the KPIs, the variant that earned it,
   papers below the cut, and papers with no KPI result (listed with the reason, never dropped).
   The ranking is recomputed from `results` every time; never write it by hand. Only `own` rows rank a paper:
   baselines, ablations and oracle / upper-bound rows never do (v1.36.0; table-qa ranked TabAF on its "Upper Bound"
   row and could rank TableMaster on an ablation). It warns when a top paper's best row does not name the paper's own
   method (a comparison method the authors ran, stored as `own`: set it to `baseline` with `import --table results`)
   and when a board has fewer than 10 papers (say so wherever that board is used).
3. `figures included` extracts figure URLs and captions from the cached arXiv HTML (no network).
   Papers whose text came from jina, a local PDF or the abstract get `inaccessible`.
4. `cluster-input > <review>/data/clusters/input.json`, then **one subagent**
   (`prompts/method-clusters.md`) writes `data/clusters/clusters.json` (cluster names of 6–12 words that say what the
   methods do and how they differ; to rename the clusters of an existing review without moving any paper, dispatch the
   same prompt with `rename only`); `merge-clusters` imports it,
   drops membership quotes that are not in the paper's cached text, and derives each cluster's winner
   from the ranking: the best-ranked member with a published venue (`published_venue`, filled by `paper`), else the
   best-ranked member. `rank-papers` also uses a venue as a tiebreak after KPI rank and coverage. Run `paper included`
   before step 6 so every paper's venue was looked up (`validate` warns otherwise); a venue found elsewhere (a CVF
   open-access page) is added with `set --field published_venue` and its source URL. The slide's paper line shows the
   venue and year ("CVPR 2024", or "no venue found, 2024").
5. For each cluster: `explainer-input C00x > <review>/data/explainers/C00x.input.json`, then **one
   subagent per cluster** (`prompts/explainer-slide.md`) writes `data/explainers/C00x.json`;
   `merge-explainers` quote-checks it against the winner's cached text and checks the figure label.
   **Critique each winner from the same packet**: one more subagent per cluster (`prompts/winner-critique.md`) reads
   the winner's whole text and writes `data/explainers/C00x.critique.json`: the limitations the authors state (each
   quoted) and 3–5 weak points and shortcomings they do not state (each `inferred`, with a kind, a rationale tied to
   the review's purpose, and the passage it rests on when there is one). Run it alongside the slide subagent;
   `merge-explainers` merges both files, drops declared limitations whose quote is not in the text, and clears a
   critique when the winner changes. `status` lists explainers with no critique.
   **Stated limitations of the other members**: one more subagent per non-winner member
   (`prompts/member-limitations.md`, the same packet: the member's entry under `siblings` names its full text and its
   `limits_output_path`) writes `data/explainers/limits/<P-id>.json`, the limitations its authors state, each quoted.
   Dispatch them with the slide and critique subagents; `merge-explainers` quote-checks them into the explainer's
   `member_limitations` and keeps only current members. `status` and `validate` list members with none. On a review
   whose packets were written before v1.27.0, run `explainer-input C00x` again for each cluster first: older packets
   do not name the members' text and output paths.
   Then `synthesis-input > <review>/data/explainers/synthesis.input.json` and **one subagent**
   (`prompts/review-synthesis.md`) writes `data/explainers/synthesis.json`, 3–5 evidence-backed takeaways
   that answer the review's purpose across families; `merge-synthesis` quote-checks them into `meta.json`. The packet
   carries `counts` (families, family members, ranked papers, papers on each board, members with code or a checkpoint),
   and `merge-synthesis` and `validate` warn when a count of the whole set ("7 of the 13 ranked papers", "the four C007
   members") does not match them: writers that counted the lists themselves wrote 13 for 12 and 16 for 15. Send the
   writer back with the warnings.
   Shared gaps are not takeaways. **Limitations and problems of the current approaches**:
   `limitations-input > <review>/data/explainers/limitations.input.json` (every family's members with their stated
   limitations, and the winners' weak points with kind, rationale and quote), then **one subagent**
   (`prompts/review-limitations.md`) writes `data/explainers/limitations.json`: 4–8 problems that two or more families
   share, each with one cell per family (`affected` on the authors' words or on the reviewer's reading, `addresses`,
   or `not-shown`), and 2–5 open problems from general knowledge of the field. `merge-limitations` quote-checks every
   cell against its paper's cached text (a failed cell becomes `not-shown`), drops a row left with fewer than two
   affected families, marks every open problem `outside-knowledge` and stores the section in `meta.limitations`. The
   synthesis slide shows it under the takeaways as a problem × family matrix, and the open problems in a separate
   box labelled "From general knowledge of the field, not from the reviewed papers".
   **Style.** Every text in the Explainers tab follows `prompts/ste-style.md`: ASD-STE100 Simplified Technical
   English applied about 80% of the way (short sentences, one idea each, active voice, one word for one meaning; no
   strict dictionary). The writers read it first. Each slide section has a short core text and a `detail` (40–80
   words, its own quote) that the hub shows in a "More detail" fold, collapsed by default; each takeaway has a
   `detail` too, and each slide gets a method `diagram` (3–8 boxes from the inputs to the output, drawn at the top of
   the slide; `merge-explainers` drops a broken one with a warning). `merge-clusters`, `merge-explainers`,
   `merge-synthesis` and `merge-limitations` list the texts that break a checkable rule
   (sentences over 25 words, avoided words, -ing verb forms, contractions, cluster names outside 6–12 words) under
   `style` in their report. They are notes, not errors, and `validate` does not repeat them (`--strict` stops on
   warnings). Read them: when one writer's file has more than 3 notes, dispatch that writer once more.
   **Highlights.** The slide, critique, synthesis and limitations writers each give every long text an `emphasis` list: 1 phrase
   copied from it, 2 when the text has 40 words or more, that the hub highlights (REVIEW.md bolds them; both show at
   most that many, the longest first). The merges drop a phrase that is not in its
   text. `status` lists long texts with no `emphasis` (reviews made before v1.22.0) and, after every
   `merge-explainers`, the method notes (data, output, loss, architecture, training) of each slide's paper, which
   its "In this review" strip shows and no writer marks. For those run
   `emphasis-input > <review>/data/explainers/emphasis.input.json`, **one subagent** (`prompts/add-emphasis.md`)
   writes `data/explainers/emphasis.json`, and `merge-emphasis` writes the phrases into the step-6 files and, for a
   method note, into `data/details/<P-id>.json` and the papers table (nothing else in them changes), and re-merges
   them; a later `merge-details` keeps them.
   The Explainers tab then opens with an overview slide (the review question, the families, a KPI
   chart coloured by family), one slide per family (with a "Results against other methods" table, the
   best paper's own comparison table pivoted from its step-5 result rows, and an "In this review" strip of its
   data, loss, architecture, training, KPI results and its siblings), and the synthesis slide (the family table, the
   takeaways, then the limitations matrix and the open problems). The table needs
   the baselines the paper re-reports to be extracted as rows with the caveat `baseline re-reported by <P-id>`;
   without them the slide says there is no comparison table rather than showing an ablation.
6. `validate` errors when a top paper is in no cluster or in two, when a cluster member fell out of the
   top N ("stale clusters": rerun items 2, 4 and 5), or when a cluster lacks its explainer or its winner critique. It
   warns when a member has no stated limitations or the synthesis has no limitations section, and errors when a
   matrix cell names a family that is gone or a paper outside its family (rerun `merge-limitations`). After
   `render` in step 7, read every slide in the hub's Explainers tab before reporting.

## Step 7 — report, validate, render, publish, record

1. **Review report** (the ten sections of an applied-research literature review, from the
   executive summary to the experimental roadmap; the user's spec of 2026-10-26, which the video follows too). The
   tables give most sections: the search log, the tasks, the test sets and metrics, each family's best paper on the five
   dimensions (data, representation, architecture, losses, training), where each ranked number comes from, the weak
   points, and code and the authors' weights with a readiness label. `report-input > <review>/data/report/report.input.json`,
   then **one subagent** (`prompts/review-report.md`) writes `data/report/report.json` with the judgement they cannot
   give: the top-down `verdict` (2–5 rows: for each goal of the purpose, the approach that serves it better, the
   principle that makes it better, when that holds and how well it is supported; the video opens with it, the user
   2026-10-28: "start with top down summary (which is better on what principles) before getting into details for each
   family") and the executive summary around it (problem, main uncertainty, next step), the requirements (goal, task, inputs and outputs, conditions, constraints, research questions; "not
   stated in the review" where the purpose does not say), what the benchmarks leave unmeasured, what works under which
   conditions and what stays unresolved, the gaps by type (method limitation, evidence gap, engineering gap, research
   opportunity) and the roadmap (a baseline, a shortlist, 2–5 proposed experiments with the question each resolves,
   the decision it supports, its needs and its stop criteria). `merge-report` refuses it (exit 1, nothing stored) when
   a part is missing, a list item or verdict row cites no record, a verdict row names an unknown family or support, a
   roadmap paper is not included, a gap type is unknown or a number is in no record of the packet; it warns on a gap
   that claims "no work exists", a family in no verdict row and a verdict text too long for its cell. A report merged
   before v1.35.0 has no verdict: `status` marks it stale and `presentation-input` refuses it; `validate` does not. Run it after step 6 and again when
   `status` marks it stale; `validate` warns while it is missing. The hub's Report tab is off for now (`HUB_REPORT_TAB`;
   the user, 2026-10-26: "I don't want the report section right now ... after we finish the video part"): the report
   is still made, because the video draws on it. Its executive summary names no paper and compares none (the user,
   2026-10-26); `merge-report` warns on a paper name in it. `unmeasured` keeps what the comparison tables leave out
   apart from what no paper reports (the packet's `outside_tables`).
2. **Adjacent topics** (the landscape around the scope, for the hub's Adjacent topics tab): `adjacent-input --abstract-chars 200 >
   <review>/data/adjacent/input.json`, then **one subagent** (`prompts/adjacent-topics.md`) puts every excluded paper
   in one neighbouring topic (6–12, seeded from the scope's `out` items), in `in_scope` (fits, but no text / window /
   cap) or in `unrelated`, and writes `data/adjacent/topics.json`; `merge-adjacent` stores it in `meta.adjacent`. Read
   the topic names and counts before publishing; rerun after screening many new papers out (`validate` warns). On a rerun, the
   subagent may keep earlier topic names, but every paper is placed from its current `excluded_because`: a paper that sat in
   `in_scope` when it was only over the cap belongs in a topic once screening excludes it on topic (dashcam: 13 stale
   `in_scope` placements, flagged by `validate` as possible misscreens).
3. `validate --strict` must exit 0. Fix the data, not the validator.
4. `render` (it also writes `rendered/review.bib`, BibTeX for the included papers), then publish the hub and, for a decision question, write `SHORTLIST.md`, both as
   `outputs.md` describes. `render` keeps the hub under the 16 MB artifact limit by itself and reports what it left
   out (`hub_trim`); if it exits 1 (`hub_over_limit`), the hub cannot be published: report the largest parts it names
   to the user, do not publish, and do not hand-edit `rendered/hub.html`.
5. Update `<home>/PROJECT.md` (status, results history; create it if missing), commit the review folder,
   and report to the user as `outputs.md` describes. When the person did not opt in to the presentation, offer it
   in one line (step 8, with its cost).

## Step 8 — presentation (opt-in: it costs tokens)

Run it only when `meta.presentation.wanted` is true (`presentation --minutes N`, 20 by default; `status` shows step-8
rows only then). It turns the finished review into a narrated explainer video that covers the **ten sections of an
applied-research literature review** (the user's spec, 2026-10-26; the same sections as the hub's Report tab):
executive summary; problem, scope and requirements; search strategy and method; background, tasks and taxonomy;
datasets, benchmarks and metrics; method families and technical analysis; comparative results and evidence quality;
practical feasibility and reproducibility; synthesis, limitations and gaps; recommendations and experimental roadmap.
The chapter guidelines are distilled in the two prompts: one chapter answers one question; times sum exactly; every
number traces to a review record. Its inputs are the merged step-6 outputs and the step-7 review report (`merge-report`
first), never full texts. Two rules from the user (2026-10-28) shape every plan and script: **top-down first** ("start
with top down summary (which is better on what principles) before getting into details for each family": chapter 1
shows the report's verdict, and every later chapter is the detail under one of its lines) and **say each thing once**
("do not repeat sentences and even subjects/claims": the first 30-minute video said one shared caveat in 6 chapters
and held 26 sentence pairs that said the same thing again, because the prompts asked for each key message twice and for
the evidence chapters to gather the caveats).

1. `presentation-input > <review>/data/presentation/plan.input.json`, then **one subagent**
   (`prompts/presentation-plan.md`) writes `data/presentation/plan.json` (`schema` 2): the outcome, the organizing
   axis, 1–3 headline answers, the recurring example (with the `chapters` that introduce and revisit it: at most 5)
   and one chapter card per chapter (question, the review `sections` 1–10 it covers, start and end seconds, evidence
   refs, the `claims` it states, family ids, data scenes, visual, `opening`, engagement, takeaway, transition). The
   packet's `allocation` gives seconds per section (20 minutes: 100, 40, 30, 70, 60, 470, 130, 80, 90, 130: the methods
   start at 5:00; 30 minutes: 120, 60, 45, 120, 75, 690, 210, 120, 135, 225; the first 30-minute video reached them at
   10:16, and an outside review asked for a mechanism within minutes). `claims` lists the takeaways, problems, open
   problems and report items (`TK`, `L`, `OP`, `VD`, `U`, `CN`, `G`, `X`) whose point the chapter states: the merge
   refuses a claim on two cards. The plan merge warns when no card lists claims, when two cards have the same takeaway,
   when the recurring example has no `chapters` or more than 5, when two chapters in a row open the same way, and
   when more than a third open with a question: send the plan writer back for those warnings before any script writer
   runs. A card names only what the narration will say aloud: 2–4 explanation steps, 2–5 supporting
   records and the one qualification that changes the claim; the hub holds the rest. **Each section's data scenes**
   get a scene of their own, listed in the card's `summaries` and drawn from review data: `executive-summary` and
   `verdict` (1: the verdict table, one row per goal, revealed row by row),
   `requirements` and `scope` (2), `search` (3: sources, dates, terms, walks, criteria, known gaps), `tasks` (4),
   `benchmarks`, `metrics` and `unmeasured` (5), `standing` (6: the first scene of every family chapter, where the
   family's members rank on each KPI comparison beside its leaders, before the family is described; the user,
   2026-11-01; the plan merge adds it and the script merge refuses a family chapter that does not open on it),
   `dimensions` (6: a family's representative paper on data, representation,
   architecture, losses and training; every section-6 chapter about a family), `provenance` (7: reported by the
   paper / verified on a leaderboard / reproduced) and `conditions` (7), `feasibility` (8: can a team evaluate the
   authors' weights, with their licence and size, and can it retrain with their code; labels "evaluate now; code
   found", "evaluate now; no code found", "code only: train first", ...), `gaps` (9, by type) and `roadmap` (10,
   labelled "Proposed experiments, not conducted"; at most three experiment cards per scene); `weak-points` is an
   optional section-7 visual. The task, test-set and metric tables
   show the tasks by number of included papers with their main test sets, the most used test sets (the KPI ones always,
   at most 2 more per task) and the most reported metrics (the KPI ones always), 8 rows each with paper counts and a KPI
   tag. `merge-presentation-plan` refuses the plan (exit 1, nothing stored) when `schema` is not 2, there is no review
   report, the chapters are not contiguous from 0 or do not end at the target, a section is on no chapter, a family is
   in no chapter and not in `folded_clusters`, a data scene the review has data for is on no chapter (or gets under
   20 s; 10 s in a video under 10 minutes), a section-6 family chapter lacks `dimensions`, or a visual is not one of
   the fixed kinds or lacks its refs; it warns when the executive summary does not open the video or the roadmap does
   not close it, and drops unknown evidence refs, bad engagement kinds and reasonless deep dives. The plan, not the
   script writers, places every engagement moment (about 2–3 predictions per 20 minutes). Re-merging drops the
   scripts of chapters that left the plan. A plan merged before v1.34.0 (eight chapter functions, schema 1) still
   renders and builds its video; `status` marks it stale until it is re-planned.
2. For each chapter: `script-input --chapter CH## > <review>/data/presentation/script/CH##.input.json`, then **one
   subagent per chapter**, in parallel (`prompts/presentation-script.md`), writes `data/presentation/script/CH##.json`:
   scenes with narration sentences, a visual from the packet's `visual_options` with reveal steps keyed to sentence
   indexes, a claim type (reported / synthesis / action / illustrative), sources and pauses, plus pronunciation
   respellings; each writer runs `check-script --chapter CH##` (the merge's checks, read-only) before it returns.
   The time estimate is calibrated on the voice: 152 words a minute of the spoken text (respellings applied) plus
   0.3 s between sentences and the pauses (fitted on 36 measured chapters; 1.2% mean error on 6 held-out ones).
   **One idea per sentence:** a listener cannot re-read, so
   the writers keep most sentences at 6–14 words, say aloud only the caveat that changes the claim and put exact
   numbers, full names, lists and the other caveats on screen. `check-script` and `merge-script` list sentences that
   carry too much under `density` (over 18 words, or a load of 3+: clause marks, names after the first, result
   numbers after the first); they are notes, not errors. When one chapter has more than 3, send it back to its writer
   once with them. The first three videos had 18–29% such sentences; the rewritten replay had 0. `wording` notes work
   the same way: "board", "best paper", "catch", "N more ... on screen", "just/only" before a number, "invented" outside
   the chapter that introduces the example, and a paper name in the executive-summary chapter (the first 30-minute
   video said "board" 40 times, "hospital team" 18 times and "invented" 12 times). `repetition` notes work the same way
   and go back to the writer: a sentence that says again what the chapter or a chapter merged before it said (4+ shared
   content words that are 60%+ of the shorter sentence's; 3 at the seam of two chapters, where one ends on the next
   one's question and the next asks it again; on video v3 the rule found 29 pairs, all but 3 restated claims), and a
   scene that cites a claim the plan gave to another chapter (the executive summary may cite the claims it
   summarises). A note can match two claims that share their words ("The team can prune a model or train one." /
   "Any pruned domain model must be trained before a team can test it."): leave such a pair. The check reads words,
   not meaning: after the last merge, read the whole script once for a claim said again in other words (the first
   20-minute replay's conditions chapter opened by restating the medical and math leads of two family chapters) and
   send those chapters back too. Each packet carries the chapter's `claims`, the other chapters' (`claims_elsewhere`) and the
   narration of the chapters merged so far (`said_elsewhere`). A record id (C005, X2) in narration
   or `on_screen` is an error. Dispatch two writers first (the executive summary and one family chapter), merge them,
   read their notes, then the rest: their packets then carry those two chapters' narration. The scene page shows no record id (`_viewer_text`), sources as "short title year · Table N",
   no rank badges on KPI charts, only the columns a results table's reveals name (`col1`...), only the matrix rows
   its reveals name, and a result's reported spread beside its value ("54.35 ± 0.50"). They also warn when
   a count of ranked papers, family members or families does not match the computed counts (a writer copied "7 of
   the 13 ranked papers" from an old takeaway): send that chapter back too.
   `merge-script` stores each chapter that passes and lists the others under `rejected` (exit 1): a chapter with a
   data scene in its card and no scene of that kind, a
   number in a non-illustrative sentence that no cited record holds (rounding and counts up to 12 pass), a reported
   scene with no source, an unknown visual, ref or reveal element, an `example` visual on a non-illustrative scene,
   or estimated speech time more than 20% off the chapter. It warns when a KPI value is said on another scale than
   its board's chart (a board with any row stored in percent is drawn in percent; packets give each value's `shown`
   form): send that chapter back to its writer with the warning. Parallel writers cannot see each other's respellings
   (one wrote "F G two" for FG², another "F G squared"): when the merge warns that a pronunciation changed, settle
   it in `data/presentation/lexicon.json` (`{"term": "spoken form"}`, the orchestrator's file); its entries win over
   every writer's, `merge-script` reports them as `lexicon_fixed`, and new packets carry them. Rerun only the rejected
   chapters' writers, with the merge's messages. **The narration is spoken English with contractions: it is exempt
   from `ste-style.md`, and no merge runs style checks on it** (the density notes measure sentence length and count
   clauses, names and numbers; they do not judge word choice or grammar). Do not "fix" that.
3. `render` writes `rendered/presentation/PLAN.md` and `SCRIPT.md` and the hub's Presentation tab; republish the hub.
4. **Audio** (no tokens: a local voice). Once per machine, `voice-setup` makes a Python 3.12 venv at
   `~/.local/share/litrev/tts` (`LITREV_VOICE_HOME`) with kokoro 0.9.4, misaki 0.9.4 and mlx-whisper 0.4.3; the models
   (~330 MB voice, ~480 MB recogniser) download on the first run. Then `audio --sample` speaks three sentences (a
   respelled term, a number, a transition) to `rendered/presentation/sample.wav` and checks them; `audio` speaks every
   scene (one sentence per call, Kokoro `af_heart`, speed 1.0: a slower speed adds a phantom vowel, kokoro#344),
   joins each scene with 0.3 s between sentences and its pause, stores the measured times and word timings in
   `data/presentation/audio.json`, writes `rendered/presentation/narration.m4a` (gitignored) and checks each scene
   with speech recognition. Only changed sentences are spoken again. A scene is flagged when the recogniser's text
   is under 0.90 similar (letters and digits) to what the voice was asked to say, its last word is not heard, or the
   voice leaves a silence over 1.5 s; word error rate is only reported (spellings such as "hot pot" / "HotpotQA" give
   10–27% on clean audio). The recogniser cannot hear a wrong stress or a letter-by-letter acronym: the lexicon
   prevents those. A chapter more than 10% off its planned time is a `status` warning: shorten it (its writer again,
   with the merge's word target) or re-plan the times. Never speed the voice up to fit.
5. **Video** (no tokens; needs Google Chrome and ffmpeg): `video` draws every scene state from review data on one
   1920×1080 page (`scripts/video_template.html`: chapter label and progress bar, the visual, `on_screen` lines as a
   strip, the sources line, an "Illustrative" badge), takes one still per reveal state in one headless Chrome over
   the DevTools protocol (`scripts/video_build.py`, stdlib only; ~90 ms a still), encodes one libx264 segment per
   state and a clip per fade, joins them frame-exact, normalizes the narration to −16 LUFS (two-pass loudnorm) and
   muxes `rendered/presentation/<slug>.mp4` (gitignored) with soft captions, plus `<slug>.srt` / `.vtt` beside it.
   **The audio is the clock:** each scene lasts its recorded audio rounded up to a whole frame (the audio is padded
   to match), each reveal starts at its sentence's recorded start, a fade takes its time from the scene before, so
   nothing drifts. `data/presentation/video.json` holds the timeline and the checks: frame count, audio length = video
   length, every planned picture change found in the MP4, no layout warnings (text under 22 px, a visual that
   overflows), every picture change 0–0.5 s before its first spoken word on the voice's own clock, and, end to end,
   speech recognition on the MP4's audio (median offset ≤ 0.5 s, drift from the first to the last third ≤ 0.15 s;
   its single words can be 1 s off near numbers, so they are listed, not failed). Read the `layout_warnings` and look
   at the start, middle and end before you report. Numbers on screen come only from review records: the writers
   choose a visual and its reveals, never its content.
   **The same slides as a PowerPoint deck:** `video` also writes `rendered/presentation/<slug>.pptx` (gitignored)
   from its stills: one slide per scene, its reveal states stacked as full-slide pictures that appear one per click
   (the edit view, the slide sorter and a PDF show the last state), the narration in the speaker notes with `[click]`
   before the sentence where the video shows the next state, a fade where the video fades, one section per chapter.
   The pictures are the video's stills, so nothing in the deck is editable text. `slides` writes only the deck
   (Chrome, no ffmpeg; about 20 s once `audio` has run) and leaves the step log alone.

## Dispatching subagents

Dispatch each subagent as the agent type in the table below, not general-purpose. These lean types start at about 8k
tokens of context instead of 34k, which cut step-5 input tokens by 36% at equal recall. If the skill was installed as a
plugin, the types are prefixed `reviewing-literature:`. If a type is missing (agent definitions load at session
start; copy `<skill-dir>/agents/*.md` to `~/.claude/agents/` when using the bare skill), fall back to general-purpose.

Send each subagent one message: "Read `<skill-dir>/prompts/<file>` and follow it. Inputs: …", where
`<skill-dir>` is the folder holding this SKILL.md and the inputs are the ones the file's Inputs section
lists. Write the inputs shared by a step once and reuse them for every agent of that step: `purpose`
for steps 0 and 3; `setting`, the allowed dataset and metric names with aliases, and the benchmark names (from
`list --table datasets|metrics|benchmarks`) for step 5. A paper's full-text method is in
`cache/fulltext/P00x.meta.json` (`fulltext` also prints it).

| Step | Agent type | Prompt file | Output |
|---|---|---|---|
| 0 | `litrev-reader` | `prompts/survey-taxonomy.md` | `data/seed_taxonomy/P00x.json` |
| 1 | `litrev-screener` | `prompts/screen-papers.md` (one per packet) | `data/screening/batch-NN.json` |
| 3 | `litrev-dataset` | `prompts/dataset-record.md` | `data/datasets_step3/<slug>.json` |
| 5 | `litrev-details` | `prompts/method-details.md` | `data/details/P00x.json` |
| 6 | `litrev-reader` | `prompts/method-clusters.md` | `data/clusters/clusters.json` |
| 6 | `litrev-reader` | `prompts/explainer-slide.md` (one per cluster) | `data/explainers/C00x.json` |
| 6 | `litrev-reader` | `prompts/winner-critique.md` (one per cluster) | `data/explainers/C00x.critique.json` |
| 6 | `litrev-reader` | `prompts/member-limitations.md` (one per non-winner cluster member) | `data/explainers/limits/P00x.json` |
| 6 | `litrev-reader` | `prompts/review-synthesis.md` | `data/explainers/synthesis.json` |
| 6 | `litrev-reader` | `prompts/review-limitations.md` | `data/explainers/limitations.json` |
| 6 | `litrev-reader` | `prompts/add-emphasis.md` (only when `status` lists texts with no highlights) | `data/explainers/emphasis.json` |
| 7 | `litrev-reader` | `prompts/adjacent-topics.md` | `data/adjacent/topics.json` |
| 7 | `litrev-reader` | `prompts/review-report.md` | `data/report/report.json` |
| 8 | `litrev-reader` | `prompts/presentation-plan.md` | `data/presentation/plan.json` |
| 8 | `litrev-reader` | `prompts/presentation-script.md` (one per chapter) | `data/presentation/script/CH##.json` |

## Quick reference

| Need | Command |
|---|---|
| new review | `init --path … --slug … --purpose … --seed … --categories …` |
| where is this review: every step done / todo / stale, and the next command (read-only; run it first when returning to a review) | `status [--json]` |
| keyword search (arXiv) | `search --term … --category … --origin … --rationale … [--limit 25] [--engine openalex\|s2]` |
| metadata + citations + labs + published venue | `paper included` or `paper <P-ids / arXiv ids> [--no-search]` (P-ids may be one comma-separated argument; `fulltext` and `enrich` take ids the same way) |
| screen candidates against the scope | `screen-input [--ids …] [--status included] [--needs-text] [--batch 150]` → subagents → `merge-screening [--rescreen] [--batch NN]` |
| manual screening (single papers, user requests) | `screen --id P0xx --decision included\|excluded --reason "…"` |
| remove a record | `delete --table <t> --id <id>` |
| dataset facts for step 3 | `dataset-info --name "<name>"` |
| archived leaderboards (once per machine) | `pwc-index` |
| citation walk | `references P00x --limit 50 [--from-fulltext] [--no-search] [--all-years]` · `citations P00x [--limit 100]` |
| next hop of forward walks (repeat with `select` + `screen` until `converged`, max 10) | `snowball [--limit 100] [--max-hop 10]` |
| apply caps | `select` · preview without writing: `select --preview 30` |
| what may be missing (adds candidates; rerun empty reference walks) | `coverage [--report-only] [--no-fetch] [--limit 100] [--survey-limit 25]` |
| why is a paper not in the review | `why-missing "<title or arXiv id or P id>"` |
| scope agreement (step 0) · decisions log | `scope --in .. --out .. --protocol .. --notes ..` · `decide --decision .. --reason .. --kind .. [--refs ..] [--by agent]` · `decide --list` |
| fold a candidate into its canonical record | `fold --table metrics\|datasets\|benchmarks --from <id> --into <id> [--variant-from-name] [--keep-alias]` |
| cached text · the open-access journal version instead of an older arXiv text | `fulltext included` · `fulltext <P> --published` |
| code / checkpoint / labs / old leaderboards | `enrich included` |
| bulk records | `import --table <t> --file records.json` (without `id`, matches exact name/alias only; prints `created X` / `updated X` per record) |
| does a JSON file parse (subagents checking their output) | `check-json <file>` (read-only; says the line and column that break) |
| one field | `set --table <t> --id <id> --field <f> --json '<literal>'` |
| self-check one step-5 file | `check-details P00x` |
| step-5 gaps (missing / invalid files) | `details-status` |
| merge step-5 files | `merge-details` |
| mark an evaluation / benchmark paper (never ranked) | `set --table papers --id P0xx --field paper_role --json '"evaluation"'` + `paper_role_reason` |
| step 6 KPIs | `kpi --suggest` · `kpi --add (--benchmark B…\|--dataset D…) --metric M… [--metric-variant …] --split … --rationale "…"` · `kpi --list` · `kpi --remove K…` |
| step 6 ranking | `rank-papers [--top 10]` |
| step 6 figures | `figures included` |
| step 6 clusters | `cluster-input` → subagent → `merge-clusters` |
| step 6 explainers + winner critiques + members' stated limitations | `explainer-input C…` → slide subagent + critique subagent + 1 member-limitations subagent per sibling → `merge-explainers` |
| step 6 synthesis | `synthesis-input` → subagent → `merge-synthesis` |
| step 6 limitations matrix (synthesis slide) | `limitations-input` → subagent → `merge-limitations` |
| step 6 highlights for long texts that have none | `emphasis-input` → subagent → `merge-emphasis` |
| adjacent topics (screened-out landscape) | `adjacent-input` → subagent → `merge-adjacent` |
| review report (the ten sections; the video draws on it; the hub tab is off for now) | `report-input` → subagent → `merge-report` |
| presentation opt-in (step 0 question) · step 8 plan · scripts | `presentation --minutes N [--audience ..] [--by agent]` / `presentation --off` · `presentation-input` → subagent → `merge-presentation-plan` · `script-input --chapter CH##` → subagent per chapter → `merge-script [--chapter CH##]` |
| step 8 script self-check (writers) · voice venv (once per machine) · narration audio | `check-script --chapter CH##` · `voice-setup` · `audio --sample` then `audio [--chapter CH##] [--voice am_michael] [--no-asr] [--force]` |
| step 8 video (Chrome + ffmpeg; captions; sync check; the slides as a .pptx too) · the .pptx alone (Chrome) | `video [--workers 4] [--no-sync]` · `slides` |
| check | `validate [--strict]` |
| outputs | `render` → tables/*.md, csv/*.csv, review.xlsx, REVIEW.md, hub.html |
| inspect | `list --table <t> [--status …]` · `show <id>` |

## Common mistakes

- Filling dataset sizes, licences, leaderboard existence or affiliations from memory. Every such
  cell needs a source URL and status; `validate` fails otherwise. If you only remember it, the
  status is `not-found` until a source is fetched.
- Writing "No leaderboard" when none was found. Not found → status `not-found` with value null;
  the hub and tables read a null value as `unknown`. A `not-found` note that carries a value fails `validate`.
- Hand-editing `rendered/` or the derived fields. Change `data/` through the CLI and re-render.
- Choosing papers you already know and searching for them by name. Terms come from the seed and the
  surveys; selection is recorded by `select` with a reason. Paper-name searches are allowed only as
  `category: concept` terms with a rationale that says why. A paper the user names is added with
  `paper <arxiv-id>` and `screen --decision included --reason "requested by the user"`.
- Letting a subagent read anything but its cached text for step 5, or fill details from an
  abstract. The quote check will delete those cells; the paper then shows `not-found` where a
  careful read would have found the passage.
- Minting a second hub URL for the same review. Republish in place.
- Summing frames with sequences, or copying the best variant's score to every variant.
- Editing `data/*.json` directly because a command seemed missing. Use `screen`, `delete`, `set`,
  `import` (with `id` to update); if something is still missing, say so in the report.
- Treating a seminal-exemption inclusion as a bug. It is additive by design; set `--max-seminal 0`
  when the user's paper count is a hard total.
- Choosing a cluster's best paper by hand. The winner is derived from the KPI ranking; if it looks
  wrong, fix the KPIs or the results, not the cluster.
- Writing an explainer from memory or from the abstract. Every sentence needs a quote from the
  winner's cached full text.
- A weak point that restates a declared limitation, or one that is generic ("more experiments would help"). Each
  names the table, dataset, setting or assumption it is about, and why it matters for the review's purpose.
- Picking a KPI split label without checking `kpi --suggest` for the same split under another label.
