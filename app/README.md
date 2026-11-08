# Litrev App

A local web app (macOS) that runs the `reviewing-literature` skill for people who don't use the Claude Code CLI.
Each skill step runs as a headless Claude Code session on the user's own Claude login. The app shows progress,
asks the skill's questions as forms, pauses on usage limits and resumes. Skill mode is unchanged.

Status: beta. A small team uses it daily on Apple Silicon Macs. Report problems at
https://github.com/orshij/reviewing-literature/issues.


## Install and use

The app runs the literature-review skill for you from a page in your browser. Everything runs on your Mac, on your
own Claude account; nothing is sent anywhere except to Claude and the paper sources (arXiv, OpenAlex, Semantic
Scholar, Hugging Face).

### Before you install

1. A Claude account with Claude Code: Team, Pro or Max.
2. A Mac with macOS 13 or newer and about 2 GB free (about 4 GB if you also want narrated videos).

### Install (once, about 15 minutes)

1. Download the installer: open Terminal (Spotlight: type Terminal) and run

   ~~~sh
   curl -fsSL -o ~/Downloads/install.sh https://raw.githubusercontent.com/orshij/reviewing-literature/main/app/install.sh
   ~~~

2. Run it:

   ~~~sh
   bash ~/Downloads/install.sh
   ~~~

   It goes through nine steps and says `ok` for each one that is already done. It may ask you to: install Apple's
   Command Line Tools (a macOS window: click Install and wait); log in to Claude in your browser. It installs Claude Code, the reviewing-literature plugin, uv (a Python
   installer), the app and a launcher, then checks everything. You can run it again at any time; it skips what is
   done.
3. When it says `Ready`, it opens Litrev in your browser. Next time, open **Litrev** from Applications in your home
   folder, or Spotlight (type Litrev).

### First run

If something on the Mac is not ready, a "Before you start" panel lists it. Problems block a review, warnings don't:

| Row | What to do |
|---|---|
| Claude login: Problem | Press **Log in**. A Terminal window opens; finish the login in the browser page it opens, then press **Check again**. |
| Claude Code: Warning "newer than tested" | Nothing. If a step later stops at startup, run the update (below). |
| Archived leaderboards: Warning | Optional. **Build it** downloads about 190 MB once; reviews then include historical Papers with Code leaderboard rows. |
| pdftotext: Warning | Optional. With Homebrew: `brew install poppler` in Terminal. |
| Reviews folder: Problem | Pick a folder outside iCloud Drive in Settings (the default `~/LitReviews` is fine). |
| Video tools: Warning | Only for narrated videos (step 8). Press **Install voice tools** (about 1.6 GB, a few minutes). Install Google Chrome if the row lists it, and run `brew install ffmpeg` in Terminal if it lists ffmpeg. "Checking that the voice tools run" goes away by itself after about 10 seconds. If it says the voice tools "do not run", press **Install voice tools** to repair them; if they still do not run, delete the folder the row names and press it again. |
| OpenAlex API key: Warning | Strongly recommended. Without a key OpenAlex gives 1,000 credits a day, shared by everyone on your network, and one review needs more. Get a free key (10,000 a day) at https://openalex.org/settings/api and paste it in Settings → Paper sources → OpenAlex API key. |

Settings → Doctor runs the same checks at any time.

### Running a review

- **New review**: a topic or seed paper, what the review is for (one or two sentences), the paper cap (default 40;
  the form shows an estimate) and the year window. The app then runs the skill's steps 0–7, one at a time.
- **What the app asks you**: one question, the review's scope (what is in and out), as a form with Claude's
  proposal filled in (edit it or accept it; your answer is recorded as yours). The review waits for it, and you
  get a notification.
- **What Claude decides itself**, so the review does not stop again: areas next to the scope (in or out), included
  papers that look off-topic, the paper cap (raised at most once, by at most a quarter), and which benchmarks and
  metrics rank the papers. Each call is listed on the review's **Decisions** tab (and the hub's Decisions page)
  with its reason. To change one, ask in the Ask Claude tab, for example "undo DEC041".
- You can close the browser tab; the review keeps running while the Mac is awake. If the Mac sleeps (lid closed),
  the review picks up from where it was when it wakes.

### Usage limits and pausing

Reviews run on your Claude plan's usage. A review costs roughly what about $30 plus $0.75 per paper would cost on
the API (the app shows this as an "API-equivalent estimate"; on a plan it is usage, not money). A 40-paper review
does not fit in one 5-hour usage window. When the limit is hit, the review shows "Paused: Claude usage limit,
resumes at HH:MM" and continues by itself at that time. **Stop** pauses a review; **Resume** continues it. One
review runs at a time.

### Asking Claude and finding new papers

- **Ask Claude** (a tab on each review): ask about the review in plain words ("why isn't <paper> in the review?",
  "which papers use dataset X?") or ask for a change ("add this paper"). Claude answers from the review's data. When a
  change needs the later steps (a new paper needs steps 4–7), the reply offers a button that runs them. While a step
  is running, Claude can only read the review: the tab shows **Read-only**; send the change again once it is gone.
  **New conversation** starts afresh; the earlier messages are saved in the review's `cache/app` folder.
- **New papers** (a tab on each review): **Find new papers** shows how many requests a check needs, **Check now**
  searches for papers published since the last check, and Claude recommends each one. Tick the ones to add (the app
  raises the paper cap when they don't fit) and confirm; steps 4–7 then run for them. Publish again afterwards.
- The header shows how much of your 5-hour and weekly Claude usage is used. Starting a new review asks first when
  the weekly figure is above 80%.

### Publishing the hub

The hub is the review's single-page summary (`rendered/hub.html`; open it in the Hub tab). **Publish** makes it a
private page on claude.ai:

1. Press **Publish**. A Terminal window opens with Claude Code; the app shows "Waiting for your approval in the
   Terminal window".
2. In that window, approve the publish (one click). The first time, Claude Code also asks whether you trust the
   reviews folder: answer yes.
3. The link appears in the app a few seconds later. Close the Terminal window. Share the page from claude.ai if you
   want others to see it.

Publishing again (**Republish**) updates the same link. If you close the window before approving, the app says so
and you can press Publish again. A claude.ai page can be at most 16 MB: the skill's render makes the hub fit (it
shortens the records of papers that are not included first, then result rows off the KPI boards) and the hub says
what it left out. If it still cannot fit, Publish shows render's message, which names the largest parts.

### Where reviews live

In `~/LitReviews/reviews/<name>/`. (If you already keep skill reviews in `~/code/literatureReview/reviews/`, the
app uses that folder instead.) `rendered/` has the outputs:
`REVIEW.md`, `review.xlsx`, `csv/`, `review.bib` and `hub.html`. `data/` is the review's data; `cache/` holds
downloaded papers and the app's own state (`cache/app/`). Keep the folder out of iCloud Drive.

### Claude Code still works

The same reviews open in Claude Code: in a Claude Code session, ask "continue the review in
~/LitReviews/reviews/<name>" (or `/reviewing-literature`). A review can move between the app and Claude Code at any
time. While a Claude Code session is open in a review, the app shows "A Claude Code session is open in this
review"; finish one before running the other.

### Updating

When a new version is announced, press **Update** in Litrev's header. It refuses while a review step, a refresh
or a chat answer is running, and names what is running. When it finishes, Litrev restarts with the new version
in a new browser tab. If the button fails, open Terminal and run the command `install.sh` printed at its end:

~~~sh
bash ~/.claude/plugins/marketplaces/litrev/app/update.sh
~~~

(If that path does not exist, run `bash ~/Downloads/install.sh` again: it prints the right one.) The update stops
when a review step or a publish is running; let it finish or stop it, then run it again. Open Litrev afterwards.

### Troubleshooting

| What you see | What to do |
|---|---|
| Litrev does not open | In Terminal: `~/.local/bin/litrev-app doctor`. Fix what it marks FAIL, or run install.sh again. |
| "Claude Code is not logged in" on a review | Settings → Doctor → **Log in**, then **Resume** the review. |
| "Paused: Claude usage limit" | Nothing: it resumes at the time shown. |
| "A Claude Code session is open in this review" | A Claude Code window is working in it. Finish or close that session, then Run. |
| Publish keeps waiting | Approve in the Terminal window. If you closed it, the app says so within a few seconds; press Publish again. |
| A step failed | Press **Retry**. If it fails again, open an issue at https://github.com/orshij/reviewing-literature/issues with the message shown. |
| install.sh: "git cannot read orshij/reviewing-literature" | Check your internet connection, then run install.sh again. |

## Use it

    uv tool install --force --python 3.12 ~/code/reviewing-literature/app
    litrev-app open          # starts the server if needed and opens the page
    litrev-app stop          # running step sessions keep going; the next start adopts them
    litrev-app serve         # foreground, for debugging (logs to the terminal)

The page lives at `http://127.0.0.1:8765` (or the next free port). It only opens through the link `open` prints,
which carries a per-launch token; links from an earlier launch stop working. If the browser blocks `localStorage`,
the page loses the CSRF secret on a reload and shows "Open Litrev again": open the link again.

| Setting (Settings page) | Default | What it does |
|---|---|---|
| Reviews folder | `~/code/literatureReview` if it has `reviews/`, else `~/LitReviews` | `LITREV_HOME` for every session |
| Subagents at once | 3 | parallel readers inside one step (429s start around 6) |
| Model for step 5 | the skill's default | `litrev-details` model, the most expensive step |
| Model for step sessions | Claude Code's default | `--model` for each step session |
| Effort | Medium | `--effort` for each step, refresh and chat session; their subagents use it too. "Claude Code's default" uses your own `/effort` setting |
| Keep a question open for | 30 min | after this the session parks; the answer starts the next session |
| Email for OpenAlex | none | `LITREV_MAILTO` |
| OpenAlex / Semantic Scholar keys | none | stored in the macOS Keychain (service `litrev-app`), never in files |
| Skill folder (Advanced) | the installed plugin's skill | must be yours and writable only by you (with its `scripts/` and `scripts/litrev.py`), and no parent folder may belong to another user or be writable by others without the sticky bit, else 422: the app runs its scripts as you; saved as the real path (symlinks resolved) |

**Narrated video (optional, step 8).** Tick "Also make a narrated video" on the New review form, or press **Make
video** on a finished review. It costs about USD 18-34 of extra usage for 20 minutes (the default length); the voice
(Kokoro) and the video (Google Chrome + ffmpeg) run on your Mac. Before the first one, open Settings → Doctor and
press **Install voice tools** (about 1.6 GB, a few minutes), and install Google Chrome and `brew install ffmpeg` if the
doctor lists them. The **Video** tab plays the result with captions and has a **Download the slides (PowerPoint)**
link: one slide per scene, its reveals on clicks, the narration in the speaker notes. The files are in the review
folder under `rendered/presentation/`.

Settings are in `~/Library/Application Support/LitrevApp/settings.json`; `server.json` there (mode 0600) holds the
running server's port, pid and token. `open`, `serve` and `stop` trust a server only when its pid is this user's and
`/health?n=<nonce>` answers `proof` = HMAC-SHA256(token, nonce), so another local listener can't pose as Litrev.

## Web app (`src/litrev_app`)

| Module | Role |
|---|---|
| `config.py` | settings, Keychain keys (written through `security -i` on stdin), the engine's `AppConfig` and the problems that stop sessions |
| `bus.py` | in-memory events: a ring of recent activities per review, live subscribers, SSE |
| `scheduler.py` | one step session at a time across all reviews; paused reviews resume at their time; sessions left running by an earlier launch are adopted (alive) or classified from their events file (dead); Run/Resume/Retry/Stop; macOS notifications |
| `locks.py` | writers the app didn't start: holders of `cache/litrev.lock`, and `claude` processes whose working folder is in the review (found with `ps`, because `lsof -c claude` matches nothing) |
| `security.py`, `server.py` | `127.0.0.1` only; per-launch token → HttpOnly SameSite=Strict cookie plus a CSRF secret only in the `/auth` redirect's fragment (`/#k=…`, never in `GET /`; the page keeps it in `localStorage`, whose origin includes the port, unlike the cookie's); every `/api/*` request needs it as `X-CSRF` (the two event streams `/api/events`, `/api/reviews/<slug>/events` take `?k=`), else 401; Host check (DNS rebinding), Origin + `X-CSRF` on writes (403); `/health?n=` HMAC proof for the launcher; strict CSP; the hub served with `sandbox allow-scripts allow-popups allow-downloads`; router, settings routes A page opened on `localhost:<port>` is redirected to `127.0.0.1:<port>`, where the secret is. |
| `api_reviews.py`, `api_checkpoints.py` | reviews list/create/detail, buttons, live events, hub; the open question, answers (recorded through litrev, attributed to the user), KPI suggestions |
| `cli.py` | `litrev-app serve / open / stop`; `alive()` trusts a server.json only with this user's pid and the `/health` token proof |
| `static/` | `index.html`, `app.css`, `app.js` (page core `window.LR`, reviews, new review, settings), `review.js` (review page, Decisions tab), `asks.js` (question forms); vanilla JS, no build step, no innerHTML |

Feature modules add routes with `register(router)` (listed in `server.FEATURES`) and page code with scripts in the
`<!-- feature scripts -->` block of `index.html` (`LR.pages`, `LR.reviewTabs`, `LR.askForms`, `LR.features`).

## Engine (`src/litrev_app`)

| Module | Role |
|---|---|
| `locate.py` | finds `claude`, the skill (installed plugin, else `~/.claude/skills`), the reviews root |
| `claude_cmd.py` | session argv (`-p … --permission-mode dontAsk --permission-prompts none --strict-mcp-config …`), environment without API keys and with `LITREV_APP_REVIEW=<review>` for every session (litrev.py then refuses `init` and any `--review`, `import --file` or `check-json` path outside the review; the app's own litrev calls never carry it), permission settings (`disableAllHooks`, writes and reads only under the review and the skill, no bare `Glob`/`Grep` rule: spikes/RESULTS.md, F5, `WebFetch` only for steps 3-5 (`WEB_STEPS`: dataset homepages, eval.ai, checkpoint READMEs; not chat, refresh or the other steps), loop tools denied), `--agents` generated from the skill, subagent command rules, MCP config |
| `runner.py` | runs a session in its own process group, tails stream-json byte-safely, init/idle/hard watchdog, SIGINT→TERM→KILL stop, `caffeinate -i` |
| `events.py` | stream-json → activity items (litrev commands, subagents, denials, questions, rate limits, results) |
| `outcome.py` | classifies the ending on the **last** of possibly many `result` events; usage-limit reset times from `rate_limit_event`, synthetic limit messages or the text |
| `ask_mcp.py` | the `ask_user` MCP tool: asks only the scope (and the refresh form; any other kind gets `DECIDE`, Claude's call), validates the proposal, waits for the app's recorded answer, parks after `wait_s` |
| `checkpoints.py` | proposal shapes per kind; answers recorded through litrev with user attribution |
| `store.py` | per-review state in `<review>/cache/app/` (gitignored; skill mode ignores it) |
| `briefs.py` | the step brief (points at the skill's `app-mode.md`) |
| `orchestrator.py` | `decide_next` (the session report is the completion signal, `status` the cross-check) and `Engine` (`create_review`, `run_one`, `answer`, `stop`) |

## Troubleshooting

- **"Open Litrev from the app" page:** the link is from an earlier launch, or the page was opened by hand. Run
  `litrev-app open`.
- **A red banner about Claude Code or the skill:** the server runs, but no step can start until Settings or the
  install is fixed.
- **"A Claude Code session is open in this review":** a Terminal session has its working folder in the review. Both
  can write; litrev's lock keeps single commands apart. Quit it, or press Run anyway.
- **Port 8765 busy:** the server takes the next free port; `open` always prints the right link.
- **Server log:** `~/Library/Application Support/LitrevApp/server.log` (written when `open` starts the server).

## Tests

`python3 -m pytest -q`. They use `tests/fake_claude.py` (replays scripted stream events, writes files, talks to the
`ask_user` server as a real MCP client), `tests/web_client.py` (a real server on a free port, a client that signs in
through `/auth` like the browser, taking the CSRF secret from the redirect's fragment, and sends any Host/Origin/cookie),
the real `litrev.py` from `$LITREV_SKILL` (default `~/.claude/skills/reviewing-literature`;
no network) and `node --check` for the page scripts.

## Doctor, publish, install (M1c)

| File | Role |
|---|---|
| `doctor_checks.py` | the seven checks (Claude Code version range, claude.ai login with the API key removed, skill ≥ 1.19.0 with `app-mode.md` and agents, venv packages, pdftotext, pwc index, reviews root not in iCloud) |
| `doctor_fix.py` | fixes: log in and update Claude Code through `.command` windows; the pwc shards download + `pwc-index` as a background job |
| `doctor.py` | `GET /api/doctor` (program checks cached 60 s, `?fresh=1`), `POST /api/doctor/<id>/fix`, `litrev-app doctor` |
| `commands.py`, `proc.py` | `litrev-app sessions` (exit 3 while an app session or a mid-run review exists); full command lines from `ps` |
| `terminal.py` | writes executable `.command` files (`exec env -u ANTHROPIC_API_KEY …`, every argument quoted) and opens them; `fresh=True` (publish) creates the file new (`O_EXCL`) and owner-only (0700) |
| `handoff.py` | publish prompt (republish refusal handled), set-meta-only settings, `.command` lines (prompt first: `--add-dir` is variadic; `LITREV_APP_REVIEW` set), transcript and meta readers |
| `publish.py` | `Publisher`: render first (also when the hub is over the limit), hub ≤ 16 MiB, refuse while a writer holds the review (a step session or the scheduler, a chat message that may change the review, a refresh, `locks.outside_writers`), `publish.command` in `~/Library/Application Support/LitrevApp/sessions/<sid>/` beside its settings (out of the review, the only place the sessions' Write/Edit rules allow), watcher (Claude's set-meta, else the Artifact result in the transcript, backfilled), re-render; routes |
| `static/doctor.js`/`.css`, `static/publish.js`/`.css` | Doctor page, Settings section, first-run overlay; Publish dialog and tab, `LR.features.publish(slug)` |
| `install.sh`, `update.sh`, `launcher/Litrev.applescript` | setup and update (bash 3.2; `LITREV_INSTALL_DRY_RUN=1` prints instead of changing), the launcher app |

Development install on the author's Mac (keeps the personal skill, no plugin):
`LITREV_APP_SRC=~/code/reviewing-literature/app bash app/install.sh`.

## Chat, new papers, usage and updates (M2)

| Feature | Where | How it works |
|---|---|---|
| Ask Claude | review tab | Each message is one `claude -p` session in the review folder with the skill; the first message creates a persisted session and later ones `--resume` it. The app keeps its own record in `cache/app/chat.json`. Each message's system prompt carries the app's run state for the review (running step N, waiting, paused until, failed, done), so "what is it doing now?" is answered from the app's record. While a step or refresh session works on the review, or the review is auto-advancing, the chat is read-only (`chat_ro`: `status`, `show`, `list`, `validate`, `why-missing`, `kpi --suggest`, `kpi --list`, `select --preview`); otherwise it may change the review (`chat_rw`) as the review's only app-side writer, and a step that wants to start waits for it. A reply ending `RUN FROM STEP n` offers a button that queues steps n-7 (from the app run's own step instead when that run stopped or failed before n). "New conversation" archives `chat.json` as `chat-<time>.json` and starts afresh. |
| New papers | review tab | "Find new papers" runs `refresh --dry-run` and shows the request count; "Check now" runs `refresh`, then a short session screens the new candidates and recommends each. You tick the papers to add; the app records `screen` included for those and excluded for the rest, `decide --kind screen --by user`, and a cap raise when the additions go over the cap (otherwise step 7's validation would fail). Steps 4-7 then run for the added papers, with a standing instruction not to rerun select, snowball or coverage. Publish as usual afterwards. |
| Usage meter | header | The last `rate_limit_event` any app session streamed: 5-hour and weekly utilisation with reset times. Starting a new review asks first when the weekly figure is above 80%. |
| Update | header | `claude plugin marketplace update litrev`, `claude plugin update reviewing-literature@litrev`, then `uv tool install --force --python 3.12 <marketplace clone>/app`. `--reinstall` too (the version may not change). Refused while any session (step, refresh, chat) runs; after a successful update the server starts the new build with `litrev-app open` once it has exited, then stops itself. It never accepts a marketplace-declared command for you: if one is needed, run the update in Terminal. |

Routes: `GET|POST /api/reviews/<slug>/chat`, `POST /api/reviews/<slug>/chat/stop|reset|run-steps`,
`GET /api/reviews/<slug>/refresh`, `POST /api/reviews/<slug>/refresh/plan|start|screen|answer`, `GET /api/usage`,
`POST /api/update`.

Troubleshooting: a chat that says "the earlier conversation could not be resumed" lost its Claude Code transcript
(the app's own record stays); "Not now: …" on New papers names what is running; the usage meter shows "–" until a
session has run since the window last reset.
