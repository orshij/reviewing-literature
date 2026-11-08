# Outputs (step 7)

## Rendered files

`render` writes `rendered/tables/*.md`, `rendered/csv/*.csv`, `rendered/review.xlsx`,
`rendered/REVIEW.md` (with Scope, Decisions, Adjacent topics and Coverage sections when present), and, once a step-8
plan is merged, `rendered/presentation/PLAN.md` (the chapter table with times, the organizing logic, one chapter card
per chapter, the families folded into other chapters, the companion and the missing information) and
`rendered/presentation/SCRIPT.md` (per chapter, the scenes with estimated times, narration, visual and reveals,
sources and claim type; "Script not written yet" for the rest; the pronunciation lexicon; once `audio` has run, the
recorded times and each chapter's recorded length against its plan), `rendered/presentation/sample.wav` (`audio
--sample`), `rendered/presentation/narration.m4a` (the whole narration), `rendered/presentation/<slug>.mp4` (the
video, 1920×1080, 30 fps, H.264 + AAC 48 kHz at −16 LUFS, soft English captions) and `rendered/presentation/<slug>.pptx`
(the video's slides as a 16:9 PowerPoint deck: one slide per scene, its reveals on clicks, the narration in the speaker
notes; written by `video`, or alone by `slides`; all four gitignored) with the video's
captions `<slug>.srt` and `<slug>.vtt` (tracked), `rendered/review.bib` (BibTeX for the
included papers: `@inproceedings`/`@article` when a published venue is verified or author-reported, else `@misc` with
the arXiv eprint; keys `<lastname><year><firstword>`, made unique) and `rendered/hub.html` from `data/`. Never edit them by hand.

Outside text is kept inert: a CSV cell that starts with `=`, `+`, `-`, `@`, tab or CR (and is not a number) gets a
leading `'`, xlsx stores such cells as text, the hub data escapes every `<`, hub links open only http(s) URLs, and
BibTeX `url`/`doi` fields percent-encode `{`, `}` and `\`.

Formulas: `data/` keeps the TeX the sources carry (LaTeXML `alttext` in full texts, quotes and figure captions; `$...$` in
arXiv / OpenAlex titles and abstracts), so quotes keep matching the cached full text. Every rendered file shows it as
Unicode text instead (`tex_to_text`: `R_{srch}` → `R_srch`, `30^{\circ}` → `30°`, `$π^3$` → `π³`, `10\times 10` →
`10 × 10`). Author macros that the paper defines itself (`\ours`, `\model`) stay as written.

Every output except the CSVs links to the pipeline map ("Litrev Pipeline Atlas": the skill's steps, processes and code
files): a line under the title of `REVIEW.md`, each `tables/*.md` and `REFRESH.md`, a `%` comment at the top of
`review.bib`, an `about` sheet in `review.xlsx` (with the hub URL), and "How this review was built" in the hub sidebar.
Set `LITREV_PIPELINE_URL` to point it elsewhere, or to an empty string to leave the link out.

## Hub

`hub.html` is a single page with the data inlined, and a claude.ai artifact can be at most 16 MB. `render` keeps the
whole page under `HUB_LIMIT_BYTES` (16,000,000 bytes), measured on the page itself:

- **Lossless, always.** A paper that is not included carries only the fields the hub reads (`hub_paper`: evidence notes
  cut to value and status, the screening verdict, kind and area, the first 3 authors, only the arXiv and DOI ids, no
  false flags). Those papers, the datasets that are not included, the result rows and the discovery links are written
  as interned columns (`_pack_results`: strings, author lists, repeated dicts such as venues and screening verdicts,
  and flat dicts such as ids; the template's unpacker and `_unpack_packed` rebuild them, null values and key order
  included).
- **Result rows get what is left** (at most `RESULTS_HUB_BUDGET`). Past that, rows off the KPI boards are thinned tier
  by tier, each only while they are still too big (`hub_results`): 1. no quote; 2. only the head of the location
  ("Table 3"); 3. rows on an excluded dataset or metric, or with no dataset, left out; 4. rows that re-report another
  paper's baseline left out (each winner's comparison table is embedded apart). Rows of papers that are not included
  never reach the hub.
- **Page tiers** (`HUB_PAGE_TIERS`) for papers and datasets that are not included, each only while the page is still
  over the limit: `reasons_cut` (the status reason keeps its first `HUB_REASON_CHARS` characters; this one also runs
  before result tiers 3-4 leave out rows of included papers) and `papers_slimmed` (no url, authors, venue or citation
  count). Every paper and dataset stays in the hub, so no link points nowhere. Included papers are never touched.
- **Still too big after every tier:** `render` writes the page, adds `hub_over_limit` (the largest parts) to its
  output and exits 1, and `status` shows the render row as stale. Make the largest part smaller (fewer or smaller
  explainer figures, a smaller screened pool), then render again.

`render` reports `hub_bytes` and `hub_trim`; the hub's Overview, Results, Papers, Datasets, Decisions and Adjacent pages
say what is not shown; `status` shows the hub size in the render row. `data/`, the CSVs and the xlsx keep every row,
field and quote (measured 2026-10-26 on llm-to-domain-slm: 46,424 rows and 6,882 excluded papers made a 25.2 MB hub;
after packing and tiers 1-4 it was 14.8 MB with 21,082 rows shown; with 8,899 excluded papers the lossless packing of
dict and list cells and of excluded datasets took it from 15.25 MB to 13.99 MB with the same text on every page, and no
page tier ran). The sidebar keeps Report, Explainers, Ranking & clusters, Results,
Coverage, Adjacent topics and the theme toggle at the top level; everything else (Overview, Decisions, the search tables, Papers, Tasks,
Datasets, Benchmarks, Metrics and Method details) sits in a collapsible "Details" group that opens when one of its pages is
active. The hub opens on Explainers once step 6 has written them, else on Ranking & clusters once the ranking exists, otherwise on Overview.
Report (`#report`; off for now: `HUB_REPORT_TAB` in litrev.py, the user's call of 2026-10-26; with it on, the hub opens there once a review report is merged) is the review as the ten sections of an applied-research literature review, each with the question
it answers: 1 executive summary with the top-down verdict table (goal, better choice, principle, when it holds,
support), 2 problem, scope and requirements, 3 search strategy and method (sources, dates, terms,
walks, criteria, known gaps), 4 tasks and the family table, 5 test sets (size, real or synthetic, access) and metrics
(direction, limitations) and what the benchmarks leave unmeasured, 6 each family's best paper on the five dimensions
(data, representation, architecture, losses, training), 7 where each KPI board's numbers come from, the weak points and
what works under which conditions, 8 code and the authors' weights with a readiness label, 9 takeaways, shared
problems, typed gaps and open problems, 10 the baseline, shortlist and proposed experiments, then links to the
appendix tables. Parts the review report gives say how to make it while it is missing. Each data table is sortable
and filterable.

Coverage (`#coverage`) has a lookup box (title, arXiv id or P id; the same verdicts as `why-missing`, computed in the
page), the neighbours table (links, the included papers linked behind a fold, status in this review), one table per
walked survey and the search gaps. Rows already included are hidden; rows screened out by hand are hidden behind a
toggle. Without `data/coverage.json` the page says how to make it. Adjacent topics (`#adjacent`) maps the screened-out papers into the
neighbouring research areas (`meta.adjacent`): a bar per topic, then per topic its definition, its boundary with the
scope (the scope `out` item and decisions it falls under) and its papers by citations (8 shown, the rest folded), plus
a separate "In scope, excluded for access or cap" group; `unrelated` papers are only counted. Without `meta.adjacent`
the page says how to make it. Decisions (`#decisions`) shows the scope, the
decision log, papers screened by hand, search terms not run and papers listed but not ranked. Overview and Papers
carry a "Download BibTeX" link, built in the page with the same rules as `review.bib` (it also shows the text, for
viewers that block downloads).

Method details (`#details`) shows the top-N ranked papers once step 6 has run (sorted by rank; the full included set stays in the
Papers table), and every included paper before that. It has two views, and the last one used is remembered per browser: **Cards**, one card per
paper with data / representation / loss / architecture / training scheme as labelled sections and the quote, source
and location folded under "evidence"; and **Compare one field**, a paper | field | evidence table for one chosen
field. Both views can be filtered and sorted by rank, id, newest or cluster.

Ranking & clusters (step 6) lists the KPIs with their rationale, the top-N table (per-KPI rank and
the variant that earned it; click a KPI header to sort by that board's rank, # for the overall order; papers below the cut and unranked papers are left off this page), and one card per method
cluster with members, rationales, quotes and the derived best paper. Explainers opens with an overview slide (the review question, the method families with their
members, and one dot chart per KPI coloured by family), then one slide per
cluster (headline, a line with the family and the paper, the method diagram, then the problem, key idea, how it
works and how it differs on the left with the paper's figure on the right; with no embedded figure the right column
holds the best KPI result, the code and checkpoint links and the figure link); arrow keys or Prev/Next move between
slides, the slide number is in the URL hash, and "Expand all" opens every fold on the slide. Since v1.24.0 the
first use of each glossary term in the slide text has a dotted underline; hover or focus shows its plain meaning. Since v1.23.0 every text in the tab is written in ASD-STE100 Simplified
Technical English applied about 80% of the way (`prompts/ste-style.md`): short sentences, one idea each, active
voice, one word for one meaning. Each slide section shows its short core text, and a "More detail and source" fold
under it (collapsed by default) holds the longer text from `details` and the status, location and quote of the
short text; each takeaway has a "More detail" fold. In the synthesis, family ids in the text ("C002 (top-down
search)") show as the family's colour and name, linked to its slide, and each takeaway's first sentence is bold. "Words on this slide"
(the glossary), "Results against other methods" and "In this review" are also collapsed by default; the
limitations and weak points stay open. Below the headline, a method diagram draws the writer's `diagram`: its boxes
from the inputs (dashed) to the output (coloured), left to right, as an SVG in the hub's colours; it scrolls in its
own box on a narrow screen. Below the slide text, "Limitations and weak points" puts the limitations the
authors state (each with its quote behind a disclosure) next to the weak points and shortcomings the critique found,
each tagged with its kind and its rationale and quote behind "why"; it is labelled as the reviewer's reading, not
the authors'. REVIEW.md lists both under each cluster. Each family slide then shows "Results against other methods": the
best paper's own comparison table, rebuilt from its `results` rows (its own rows marked *this paper*, the
baselines it re-reports, numbers as printed, best per column in bold with oracle rows excluded, the review-KPI
column highlighted). The table is the paper table (`location` "Table N") holding the paper's best KPI row when that
table re-reports baselines; otherwise its comparison table closest to that KPI, and the caption says which and why.
Qualifiers the column already states ("retriever BM25") are dropped from method names, and qualifiers shared by
every row or column move to the caption. At most 10 columns (KPI metric first) and 20 rows show; the rest are
counted and left to the Results tab. Headers sort the rows: the table opens sorted best-first on the KPI column
(or its first column), a click on another header sorts by it, a second click flips it, empty cells stay last, and
Method restores the paper's row order. The sort is per viewer and is not saved. Below it, "Later papers on the same benchmark"
lists, for each review KPI board the paper is on, its own best row and every included paper that appeared after it
(newer arXiv id, else later year) with its best row on that board, its split, setting, value as printed and board
rank. These rows are not merged into the paper's columns: no later paper reports exactly the same split and setting,
and the board is the review's only curated comparability set. It ends with an
"In this review" strip: the best paper's data, output, loss, architecture and training notes, its rank on
every KPI, and its siblings' ranks and availability. The last slide is the synthesis: a family-by-family
table (supervision and data, prior or backbone, output, best KPI result, code and weights), the
evidence-backed takeaways, and "Limitations and problems of the current approaches": a problem × family matrix
(rows: the problems two or more families share, each tagged with its kind; columns: the families, linked to their
slides; cells: ● affected, the authors say so · ◐ affected, the reviewer's reading · ✓ the family addresses it ·
— not shown; pointing at a cell or giving it keyboard focus shows the paper and its quote or rationale in the
page's own tooltip, since the browser's `title` tooltip did not show in the published hub). Under it, one entry per
problem with its text, a "More detail" fold and an "Evidence" fold that lists each cell's paper, quote and rationale. A dashed box below
lists the open problems that no reviewed approach solves, labelled "From general knowledge of the field, not from
the reviewed papers"; they carry no quote. REVIEW.md shows the matrix as a table with the same marks, the problems as
a list and the open problems under the same label. In the long texts (slide fields, limitations, weak points,
takeaways, limitations problems, open problems, and the method
notes in the "In this review" strip) the hub highlights the longest phrase of each text's `emphasis`, or the two
longest when the text has 40 words or more (`EMPH_SHORT_WORDS`); REVIEW.md bolds the same phrases (it does not show
the method notes). The strip shows each method note's first sentence and folds the rest: the phrases are chosen on
the whole note, and each part marks the ones inside it. The slide's glossary marks go on into the strip. Explainer figures are embedded as `data:` URIs within 800 KB
each and 4 MB in total; otherwise the slide links the figure and says why.

Presentation (`#presentation`, top level, only once a step-8 plan is merged): the audience, outcome, organizing axis,
headline answer and recurring example, a chapter table (time, question, visual, engagement, script status), then one
box per chapter with the question and takeaway, a folded "Chapter card" and an open "Script" table (estimated time,
narration and on-screen text, visual with its reveals, sources, claim type; illustrative claims have a dashed tag),
then the folded families, the companion, the missing information and the pronunciation table. Once `audio` has run, the
intro gives the recorded length against the plan and the number of flagged scenes, each chapter heading its recorded
length and drift, and the script table the recorded scene times.

Results is one leaderboard per metric. Rows are ranked only against rows with the same dataset,
normalised split (same-area or cross-area) and metric variant. Defaults are the best row per paper,
with baselines re-reported by other papers hidden; toggles show either, plus a raw all-rows table.
A sticky bar holds the metric (previous and next), dataset and split selectors and a filter. A
board index is grouped by dataset; single-paper boards are opt-in. Each board carries Previous,
Next and Index links; keys `j`/`k` move between boards and `[`/`]` between metrics.

Publish rules:

- Load the `artifact-design` skill first, then publish `rendered/hub.html` from that path.
- Publish with `capabilities: {downloads: true}` (the BibTeX link saves through it; the viewer blocks plain
  downloads, and `.bib` is not on its allowlist, so the file is `<slug>.bib.txt`). A republish without `capabilities`
  keeps the stored declaration.
- One review has one hub URL. Later runs republish with `url`; store it with
  `set-meta --field hub_url --json '"<url>"'` and `render` again so REVIEW.md carries it.
- `render` writes the hub as pure ASCII (non-ASCII becomes JSON or JS `\u` escapes; a test enforces it), because
  the published page can be decoded with the wrong charset. Keep template text inside `<script>` or ASCII-only;
  garbled symbols in a local preview are a real bug, not a preview artefact.
- Template changes go in `scripts/hub_template.html`. Layout gotchas: no `<nav>` element inside `main`
  (the sidebar's `nav` CSS applies to it); the mobile grid column is `minmax(0,1fr)`; headless Chrome lays
  out at a 500px minimum, so check phone width by measuring, not by screenshot.

## SHORTLIST.md

Write `<review>/SHORTLIST.md` when the purpose asks for a decision (which methods to evaluate,
adopt or reproduce). Sections, in order:

1. Read this first: the protocol differences that block ranking across papers, and the benchmark
   closest to the user's setting.
2. Evaluate first: papers with code, weights and evidence matching the user's constraints; per paper
   one line of reason and one of caveat, with code and weights status.
3. Promising, a piece missing: the paper and the missing piece.
4. Not suitable as stated: the paper and the blocking reason.
5. Evaluation plan: protocol, order of runs, metrics.

Every number in it is a cell in `results`; every code or weights claim matches the paper's `code` and
`checkpoint` notes.

## Final report to the user

Counts, the evidence-status histogram, exclusions worth a look, every `inaccessible` and `not-found`
cluster, the hub link, and the xlsx and csv paths. For a decision question, lead with the shortlist.
