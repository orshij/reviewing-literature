# Changelog

## 1.37.0 (plugin), 0.4.4 (Litrev App): first public release (2026-11-14)

The version numbers continue the internal releases, so installs that move to this repository do not see a downgrade.

What the plugin does in this release:

- **Search and screening (steps 0–1):** seed papers or a concept; arXiv, OpenAlex and Semantic Scholar search;
  citation walks (snowball) and a coverage pass; per-paper screening against the review's scope on a Sonnet
  subagent; a paper cap (default 128) filled with on-topic papers first; adjacent topics for the excluded papers.
- **Tables (steps 2–5):** linked papers, datasets, benchmarks, metrics and results with stable IDs and per-cell
  evidence statuses; dataset records from official pages; full text from arXiv HTML, open-access PDFs and
  `pdftotext`; per-paper method details (data, representation, loss, topology, training scheme); archived
  Papers-with-Code leaderboard rows.
- **Synthesis (steps 6–7):** method families with explainers and a critique of each family's best paper, KPI
  leaderboards that rank a paper only on its own result rows, a limitations matrix, and a ten-section review
  report; `validate --strict` before rendering.
- **Outputs:** Markdown, CSV, optional xlsx, and a single-file HTML hub (at most 16 MB, so it can be published as a
  claude.ai Artifact).
- **Step 8 (opt-in):** a narrated video of a finished review (about 20 minutes, local voice, Apple Silicon), plus a
  PowerPoint deck of its slides.
- **Litrev App (macOS):** runs each step as a headless Claude Code session on your own login, asks questions as
  forms, pauses on usage limits and resumes, finds new papers for an existing review, answers questions about a
  review, and publishes the hub.

- **Pipeline Atlas:** `docs/atlas.html`, a map of every step, process and code file, served with GitHub Pages;
  every rendered review links to it.

Known limits:

- Beta. The pipeline changed often in the last weeks; expect changes in output files between minor versions.
- Cost: see the README. A large review uses a large part of a weekly plan limit.
- The narrated video needs Apple Silicon. The Litrev App needs macOS.
