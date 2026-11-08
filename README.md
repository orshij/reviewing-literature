# reviewing-literature: a Claude Code plugin

Builds structured literature reviews as linked tables (papers, datasets, benchmarks, metrics, results) with stable
IDs and per-cell evidence. It renders Markdown, CSV and optional xlsx files, an HTML hub page, and optionally a
narrated video. Claude runs it when you ask for a literature review, a survey of a field, a related-work table, or a
SOTA/leaderboard comparison.

**Status: beta.** A small team uses it daily, but it changes fast. Read the cost note below before the first run.

## Install

In Claude Code:

```
/plugin marketplace add orshij/reviewing-literature
/plugin install reviewing-literature@litrev
```

Start a new session and ask, for example, "Do a literature review of monocular depth foundation models".

To update later, run both commands in a terminal, then start a new session:

```
claude plugin marketplace update litrev
claude plugin update reviewing-literature@litrev
```

## Cost and usage limits

A review runs many Claude sessions and subagents. Measured on real runs (API list prices; on a Claude subscription
this is usage-limit budget, not cash):

- In Claude Code: about **$30 fixed + $0.75 per included paper**. A 128-paper review is about $100–130 and
  110–140M tokens (most of them cheap cache reads). On a $100/month Claude plan that is about one 5-hour window and 10–17% of
  the weekly limit.
- Through the Litrev App, one 35-paper review with very large result tables cost about $333.
- The skill asks for a paper cap at the start. Start with a small cap (20–40 papers) to see the cost on your plan.

## Litrev App (optional, macOS)

A local web app that runs this skill step by step on your own Claude login, for people who do not use the Claude
Code CLI. It asks the skill's questions as forms, pauses on usage limits and resumes, and publishes the hub. It is
in [`app/`](app/README.md), outside the plugin, so the plugin install does not change. Install:

```sh
curl -fsSL -o ~/Downloads/install.sh https://raw.githubusercontent.com/orshij/reviewing-literature/main/app/install.sh
bash ~/Downloads/install.sh
```

## Requirements

- Claude Code with a Claude account (Pro, Max or Team) or an API key.
- `python3` 3.9 or newer. The core script `scripts/litrev.py` uses only the standard library.
- Optional: `pip install pyarrow` (archived Papers-with-Code leaderboards, `pwc-index`), `pip install openpyxl`
  (xlsx export), and `pdftotext` (poppler) as a full-text fallback.
- Optional, step 8 (the narrated video): Google Chrome and `ffmpeg`, and the voice venv that `litrev.py voice-setup`
  makes once per machine at `~/.local/share/litrev/tts` (`LITREV_VOICE_HOME`; needs `uv` or `python3.12`; Kokoro-82M
  and mlx-whisper, Apple Silicon only; about 1.6 GB with the models).

## Environment variables

| Variable | Purpose |
|---|---|
| `LITREV_MAILTO` | **Set this to your own email address.** Paper sources use it to contact you about your traffic. |
| `OPENALEX_API_KEY` | Strongly recommended. Without a key, OpenAlex gives 1,000 credits a day per IP address (shared by your whole network), and one review can need more. A free key gives 10,000 a day: https://openalex.org/settings/api |
| `S2_API_KEY` | Optional. Without a key, Semantic Scholar often answers with HTTP 429; the skill retries and falls back. |
| `LITREV_HOME` | Optional. Root folder for reviews (default `~/code/literatureReview`). |
| `LITREV_PWC_CACHE` | Optional. Location of the pwc-archive parquet cache (default `$LITREV_HOME/cache/pwc/`). |

Reviews are written to `$LITREV_HOME/reviews/<slug>/`, and each run updates `$LITREV_HOME/PROJECT.md`.

## Files

The skill is in `plugins/reviewing-literature/skills/reviewing-literature/`:

- `SKILL.md`: the workflow Claude follows (steps 0–8).
- `schema.md`: table schema and evidence statuses.
- `sources.md`: data sources and their limits (last checked October 2026).
- `outputs.md`: the hub page, leaderboards, the review report and the video.
- `prompts/`: instructions for subagents. `agents/`: the subagent types (`litrev-*`), also at the plugin root.
- `scripts/litrev.py`: the store, fetchers, validator and renderer; `scripts/hub_template.html`: the hub template.
- `tests/`: unit tests with fixtures, no network:
  `python3 -m pytest plugins/reviewing-literature/skills/reviewing-literature/tests -q`.

## Pipeline Atlas

[`docs/atlas.html`](docs/atlas.html) maps each step of a review: what happens, in what order, who does it (the
`litrev.py` CLI, a subagent, the orchestrating agent or you), and which code, prompt and data files it uses, with
line numbers into `scripts/litrev.py`. It is published with GitHub Pages at
https://orshij.github.io/reviewing-literature/atlas.html, and every rendered review links to it
(`LITREV_PIPELINE_URL` changes the link; an empty value removes it).

## Data sources and terms

The skill reads public metadata and open-access full text from arXiv, OpenAlex, Semantic Scholar, Hugging Face and
the Papers-with-Code archive. Respect each source's terms of use; the skill keeps to their published rate limits.

- Thank you to arXiv for use of its open access interoperability.
- Papers with Code data is CC BY-SA 4.0 (the archived copy on Hugging Face, frozen 2025-09-13).
- Paper texts are cached only on your computer, in each review's `cache/` folder. Do not redistribute them.

Claude writes the reviews from those sources. Every table cell carries an evidence status (verified /
author-reported / inferred / ...); check the cells you rely on.

## Changelog, license, security

Release notes: [CHANGELOG.md](CHANGELOG.md). License: [MIT](LICENSE). Security reports: [SECURITY.md](SECURITY.md).
