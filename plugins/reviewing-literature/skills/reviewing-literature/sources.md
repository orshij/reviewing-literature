# Data sources (verified live 2026-10-02)

What `litrev.py` talks to, what each is good for, and the limits that shaped the design. Re-verify a
row before relying on a behaviour that matters; record changes here with the date.

## Contents
- Summary table
- Per-source recipes and gotchas
- Budget rules
- Dead or unusable sources (do not retry)

## Summary table

| Source | Role in the pipeline | Key | Limits |
|---|---|---|---|
| arXiv API `export.arxiv.org/api/query` | primary keyword search (`search` default engine); authoritative metadata/abstracts (`paper`) | none | ≥3 s between requests, one connection; `https://` (http returns an empty redirect); Atom XML |
| arXiv HTML `arxiv.org/html/<id>` | full text, first choice (`fulltext`) | none | not every paper has an HTML build; large pages (~0.4 MB) |
| r.jina.ai `r.jina.ai/https://arxiv.org/pdf/<id>` | full text fallback: PDF → markdown | none | 20 req/min unkeyed; temporary 403 blocks after heavy use |
| local `pdftotext` (poppler) | last full-text fallback on the arXiv PDF | none | needs `pdftotext` on PATH |
| GitHub raw README `raw.githubusercontent.com/<owner>/<repo>/<main|master>/README.md` | released-weights links for `checkpoint` (`enrich`) | none | links to Drive/OneDrive are recorded, not downloaded |
| OpenAlex `api.openalex.org/works` | citation counts, `referenced_works`, `filter=cites:`, author institutions (`paper`, `references`, `citations`, labs) | optional `OPENALEX_API_KEY` (free, https://openalex.org/settings/api) | credit-metered, charged per request (measured 2026-10-13): a single record `/works/W…` or `/works/doi:…` = 0, a list or filter page = 1, `search=`/`title.search:` = 10; 1,000 credits/day without a key **per IP address** (a shared office network shares it), 10,000/day with a free key; resets 00:00 UTC; `mailto` no longer buys anything (polite pool ended 2026-02) but does no harm |
| Hugging Face `huggingface.co/api/papers/<arxiv>` | `githubRepo` (code), `linkedModels`, summary (`enrich`) | none | — |
| Hugging Face `huggingface.co/api/models?filter=arxiv:<id>` | checkpoints that *cite* the paper (`enrich`) | none | returns community fine-tunes too; see classification rule |
| Hugging Face `huggingface.co/api/datasets?search=` | dataset homepages / cards (step 3 subagents) | none | — |
| `pwc-archive/*` parquet on HF | historical leaderboards (`evaluation-tables`), official-code flags (`links-between-paper-and-code`), dataset metadata (`datasets`) | none | **frozen 2025-09-13**; ~190 MB; needs `pyarrow` |
| eval.ai `eval.ai/api/challenges/challenge/all/all/all` | "does an official challenge/leaderboard exist" (step 3 subagents) | none | — |
| Crossref `api.crossref.org/works` | DOI resolution only | none | 3 req/s with `mailto` |
| Semantic Scholar Graph API | fallback for citation walks OpenAlex cannot serve (`references`, `citations`), `search --engine s2` | optional `S2_API_KEY` (dedicated rate limit) | unkeyed tier is a shared global pool: often 429 under load, worked 3/3 on 2026-10-08; 429s are retried, never cached |

## Per-source recipes and gotchas

**Published venue (`published_venue`).** Derived with no extra requests from records already fetched: every
OpenAlex record of the paper that `paper` looked at (arXiv-DOI record + exact-title matches sharing an author), first
location whose `source.type` is journal / conference / book series / ebook platform with `is_published` true or
`version: publishedVersion` (`submittedVersion` journal hits such as a same-title paper in an unrelated journal are
dropped); then the arXiv `journal_ref`, then the arXiv `comment` ("Accepted to CVPR 2024", "ECCV'24", "CVPR-25").
Measured 2026-10-08: mde 8/12 included papers got a venue, dashcam 29/43; the misses are mostly CVF open-access papers
(UniDepth CVPR 2024, Depth Anything V2 NeurIPS 2024) that OpenAlex holds only as the arXiv preprint and whose
comment names no venue. DBLP is not used as a venue source (rejected by the user, 2026-10-08): it is an index, not
a venue. Its search API also sits behind a bot challenge; `sparql.dblp.org` answers.

**arXiv search.** Multi-word terms must be AND-ed word by word (`all:monocular AND all:depth …`); a
quoted phrase collapses recall to a handful of exact matches. Stop words must be left out (verified 2026-10-25): arXiv
does not index Lucene's English stop set (`of`, `on`, `in`, `and`, `for`, `with`, `the`, …), so one `all:of` clause makes
the whole AND match nothing ("chain of thought distillation small models": 0; without "of": 81). `arxiv_search_query`
drops them (`ARXIV_STOPWORDS`; "from", "via", "using", "over" are indexed and kept) and `refresh` strips them from
older stored queries. arXiv also answers a search now and then with a well-formed empty feed (3 of 45 searches in a
row on 2026-10-25; 164 / 24 / 91 results minutes later): `search` fetches an empty answer twice more, uncached, after
10 s and 30 s, and a run still empty says so in its `limitations`. `litrev.py` adds the review's
categories and a `submittedDate` window from `caps.years`. Relevance ranking is not citation-aware:
`select` therefore ranks with OpenAlex citation counts fetched through the free DOI filter.

**OpenAlex id resolution.** There is no `ids.arxiv` filter. Working route: `filter=doi:10.48550/arXiv.<id>`
(≈free) returns the *preprint* record, whose citation count is usually tiny (Depth Anything: 23).
The published version is a separate record (1179 citations) found with `filter=title.search:<title>`
(10 credits) and `openalex_pick_best`, which keeps only exact-title matches and takes the most
cited. `search` does the free step for every candidate; `paper` does the paid step only for the
papers you pass it (normally the included set). Papers from before 2022 have no arXiv DOI, so for
them only the title route works. `/works/doi:…` path lookups 404 for arXiv DOIs; use the filter.
Institutions come from `authorships[].institutions` (sometimes empty on preprint records; the
published record is usually better).

**OpenAlex `referenced_works` gaps.** Many records have an empty reference list: recent surveys (arXiv 2507.11540:
0 refs), CVF-indexed CVPR papers (DUSt3R, VGGT: `referenced_works_count: 0`). An empty list breaks both directions:
a backward walk finds nothing, and the paper never appears in any `filter=cites:` forward walk. `references` falls back
first to Semantic Scholar's `/paper/{arXiv:id|DOI:doi|s2id}/references` (unkeyed calls answered 3/3 on 2026-10-08;
a key only buys a dedicated rate limit), then to the bibliography in the cached arXiv-HTML text (the `[n]` entry layout; jina/pdftotext texts are not parsed and
the run says so), resolving entries by arXiv id, then by an arXiv `ti:` word-AND search with a normalised-title
match (≥0.93 similarity), then (unless `--no-search`) an OpenAlex title search. A citing paper whose own OpenAlex record lacks references is invisible to `filter=cites:`, so every forward walk
also pages S2 `/citations` (up to 3000 citing works, most cited first) and adds the top `--limit` works OpenAlex did
not list (matched on arXiv id, DOI or normalised title).

**Citation counts mix sources; the rule is max.** A stored count is only ever raised: `openalex_resolve` keeps a
higher S2 count over a lower OpenAlex one (an OpenAlex arXiv-preprint record had Depth Anything 3 at 2 citations vs
S2's 748, which sank it below the cap in `select`). Counts are therefore max(OpenAlex published, OpenAlex preprint, S2).

**Hugging Face checkpoints.** `filter=arxiv:<id>` lists every model whose card cites the paper. A
model counts as `official` only if its namespace matches an author name, the code repo's name or owner, or one part of a
multi-part owner (`bigcode` of bigcode-project: SelfCodeAlign's StarCoder2-15B-Instruct; generic parts such as `research`
never count) (`classify_checkpoints`); otherwise `third-party`. No hits → availability `unknown`. A third-party hit is often
a stranger's quantization that cites the method (QuIP#: GLM and Qwen quants, 2026-10-26), so `synthesis_counts` counts
only official checkpoints. With no official HF model, `enrich` reads the official repo's README: badge images
(`[![DOI](…badge…)](doi.org/…zenodo…)`, a code archive) are removed first, URL templates (`opt-[MODEL-SIZE]`) never count,
a README owner counts as the authors' only when it is one part of a multi-part GitHub owner (`relaxml` of Cornell-RelaxML;
SliceGPT's `microsoft/phi-2` is a base model under the same org), and in a weights section the link whose repo name starts
with the code repo's name wins (`nvidia/Minitron-8B-Base` over the later Mistral-NeMo-Minitron listed first).

**Several "official" repos in pwc-archive.** `links-between-paper-and-code` can flag more than one repo as official (AWQ:
lmdeploy, TensorRT-LLM, llm-awq, vLLM; measured 2026-10-26). `enrich` ranks them: the repo the paper text names first,
then an owner that matches an author, then a repo name holding the paper's short name (`_official_link_rank`). The
replay on llm-to-domain-slm moved AWQ, QLoRA (bitsandbytes → artidoro/qlora), OWQ and Effi-Code to the repo their text
prints.

**pwc-archive.** Download the shards once (curl) into
`$LITREV_PWC_CACHE` (default `<home>/cache/pwc/`, `<home>` = `$LITREV_HOME` or `~/code/literatureReview`):
`https://huggingface.co/api/datasets/pwc-archive/<name>/parquet` lists shard URLs
(`evaluation-tables` 4 shards, `links-between-paper-and-code` 1, `datasets` 1). The HF
`/rows` preview endpoint times out; use the shards. Then run `python3 scripts/litrev.py pwc-index` once
(≈7 s with pyarrow columnar ops; 155k leaderboard rows, 36k paper keys → `eval_index.json`, 130 MB).
Never convert the evaluation-tables rows with `to_pylist()`: the row struct has ~3.5k mostly-null
metric fields and the conversion ran past 30 minutes. `enrich` returns zero leaderboard rows until
the index exists. Every fact derived from it carries
`snapshot_date 2025-09-13` and a caveat; it is historical, not current.

**Full text.** arXiv HTML (LaTeXML) is preferred; `html_to_text` strips tags. If absent or tiny,
jina returns PDF text as markdown. jina can block anonymous arxiv.org access for about an hour after
heavy use (HTTP 403, `AbuseAlleviationError`, verified 2026-10-02); `fulltext` then downloads the PDF
and converts it with local `pdftotext` (poppler, method `pdf-local`; line breaks fall mid-sentence). Papers with no
arXiv id (most citation-walk finds are ACM/Springer/IEEE records) next try the open-access PDFs on their OpenAlex
record, repository copies first (method `pdf-oa`, same line-break caveat). Closed papers have none.
If all fail the cached text is abstract-only and `merge-details` forces `inaccessible` for unstated
fields. arXiv HTML puts a non-breaking space before citation brackets; `merge-details` and
`check-details` normalise whitespace, a plain `grep -F` does not.

**HTTP robustness.** A server `Retry-After` is capped at 60 s (`MAX_RETRY_AFTER`; an uncapped value
stalled one search for 35 min), and a truncated OpenAlex response (`http.client.IncompleteRead`) is
retried like a connection error. A spent OpenAlex daily budget answers HTTP 429 with "Insufficient budget" in the
body and `Retry-After` = seconds to 00:00 UTC; retrying cannot clear it (5 capped retries cost ~5 min per call, ~10 min
per snowball paper on 2026-10-13). `http_get` therefore marks OpenAlex spent on the first such 429 (or any OpenAlex
`Retry-After` over 60 s), prints one WARNING with the reset time, and answers every later OpenAlex call in that
command with 429 at once (`HOST_DOWN_UNTIL`, nothing cached). Citation walks then use Semantic Scholar (the run's
limitation says "OpenAlex daily credit budget used up"); `paper` notes the skip in `sources` and records no
"not-found" venue; `search --engine openalex` stops with an error and records nothing (a 0-result run would make the
term look searched). A burst 429 ("10 requests per second", `Retry-After: 1`) keeps the normal backoff.

## Budget rules

- OpenAlex: read `x-ratelimit-remaining` (exposed in `LAST_HEADERS`); `paper` skips the 10-credit
  title search when fewer than 100 credits remain and notes it in `sources`. A 128-paper review
  (the default cap) costs ≈ 1280 credits for title searches plus 1 credit per filter call (`paper`'s DOI
  lookups, every walk page), well over the keyless daily budget (1,000 per IP address, shared by everyone on
  that network). Set `OPENALEX_API_KEY` (free, 10,000/day); without one, expect a review to run out on day one.
- arXiv: the client enforces 3 s spacing; a 15-term search round takes ~1 min.
- All responses are cached under `<review>/cache/http/` (searches 7 days, records forever), so
  re-running a step is free.

## Dead or unusable sources (do not retry)

- **Papers With Code** (`paperswithcode.com`, `/api/v1/…`, `/sota/…`): 302 to
  `huggingface.co/papers/trending`. Shut down; data lives on in `pwc-archive`.
- **Google Scholar**: no API; scraping is against its terms and CAPTCHA-blocked after a few
  requests. Optional manual spot-check through the browser for ≤5 seminal papers, recorded as
  evidence with the Scholar URL; never automated.
- `evalai.cloudcv.org` (connection refused), old CodaLab API (404), Kaggle competitions API (401
  without credentials), Codabench (empty results for common benchmarks).
