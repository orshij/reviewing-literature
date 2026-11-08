#!/usr/bin/env python3
"""litrev.py - structured literature-review store + fetchers + renderer.

Single-file, stdlib-only CLI (openpyxl and pyarrow are optional extras).
Data model follows the linked-table spec: stable sequential IDs
(Q search term, S search run, P paper, T task, D dataset, B benchmark,
M metric, R result), evidence notes with explicit statuses on every
substantive fact, yes/no/unknown availability, Discovery Links retained
per discovery path.

Run `python3 litrev.py --help` for subcommands.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as _dt
import hashlib
import http.client
import html as _html
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

STATUSES = ("verified", "author-reported", "inferred", "not-reported", "not-found", "inaccessible", "not-applicable")
POSITIVE = {"verified", "author-reported", "inferred"}
NEGATIVE = {"not-reported", "not-found", "inaccessible", "not-applicable"}

TABLES = ["search_terms", "search_runs", "discovery_links", "papers", "tasks", "datasets", "benchmarks", "metrics", "results",
          "clusters", "explainers"]
LIST_TABLES = {"discovery_links"}  # everything else is a dict keyed by id
PREFIX = {"search_terms": "Q", "search_runs": "S", "papers": "P", "tasks": "T", "datasets": "D", "benchmarks": "B", "metrics": "M", "results": "R",
          "clusters": "C", "explainers": "X"}
# step 6 (rank, cluster, explain)
EXPLAINER_FIELDS = ["problem", "key_idea", "how_it_works", "vs_siblings"]
WEAK_POINT_KINDS = ["evaluation", "comparison", "generalisation", "assumption", "cost", "reproducibility", "data", "claim"]
FACETS = ["supervision", "prior", "output"]   # short per-family answers to the review question, shown in the synthesis table
EMPHASIS_MAX, EMPHASIS_WORDS = 3, 6   # per long step-6 text: phrases the hub highlights, and words per phrase
KPI_MIN_PAPERS = 3   # a board with fewer papers makes "rank 1" meaningless
KPI_FEW_PAPERS = 10  # rank-papers warns below this: table-qa's FinQA board ranked on 8 papers
KPI_MAX = 6
FIG_MAX_BYTES = 800_000
# "Litrev Pipeline Atlas": the steps, processes and code files of this skill. Every rendered output links to it;
# LITREV_PIPELINE_URL overrides it and an empty value drops the link.
PIPELINE_URL_DEFAULT = "https://orshij.github.io/reviewing-literature/atlas.html"
FIG_TOTAL_BYTES = 4_000_000

# Every included paper must carry one of these as an evidence note (value or explicit status).
PAPER_REQUIRED_EVIDENCE = ["labs", "relevance", "contribution", "code", "checkpoint",
                           "data", "representation", "loss", "topology", "training_scheme"]
DETAIL_FIELDS = ["data", "representation", "loss", "topology", "training_scheme"]
DATASET_REQUIRED_EVIDENCE = ["ground_truth", "size", "availability", "real_synthetic", "leaderboard"]
AVAILABILITY = ("yes", "no", "unknown")
# what a paper contributes; only "method" papers are ranked on the KPIs (an evaluation study's best row is someone else's method)
PAPER_ROLES = ("method", "evaluation", "benchmark", "survey")
DATASET_AVAILABILITY = ("public", "registration", "request-only", "restricted", "unavailable", "unknown")
TERM_CATEGORIES = ("concept", "task", "method", "dataset", "benchmark", "metric", "application")
DISCOVERY_TYPES = ("keyword", "reference", "citing", "survey", "dataset", "leaderboard", "seed")

# HTTP politeness. arXiv asks for >=3 s between requests; jina allows 20/min unkeyed;
# OpenAlex/HF have no per-second rule but 0.2 s keeps bursts polite.
HOST_INTERVAL = {"export.arxiv.org": 3.0, "arxiv.org": 3.0, "r.jina.ai": 3.0, "api.semanticscholar.org": 1.1,
                 "api.openalex.org": 0.2, "huggingface.co": 0.2}
# Search results change; paper records and full text do not (for our purposes).
TTL_SEARCH = 7 * 86400
PUBLISHED_VENUE_BONUS = 1.5   # select: a peer-reviewed paper outscores an otherwise equal preprint (one keyword hit = 2.0)
TTL_FOREVER = None
REQUEST_TIMEOUT = 60       # arXiv HTML pages can be large; 60 s covers slow links
MAX_RETRIES = 5            # 1,2,4,8,16 s backoff covers transient 429/5xx without stalling a run
MAX_RETRY_AFTER = 60.0     # seconds; cap on a server-sent Retry-After header
PWC_SNAPSHOT = "2025-09-13"  # pwc-archive last update; all facts derived from it carry this date
LITREV_HOME = Path(os.environ.get("LITREV_HOME", Path.home() / "code" / "literatureReview")).expanduser()
PWC_CACHE = Path(os.environ.get("LITREV_PWC_CACHE", LITREV_HOME / "cache" / "pwc"))
MAILTO = os.environ.get("LITREV_MAILTO", "")
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "")
S2_KEY = os.environ.get("S2_API_KEY", "")
CACHE_ONLY = False   # set by `coverage --no-fetch`: http_get serves the cache and never touches the network
# Below this many remaining OpenAlex credits we stop paying for title searches (10 credits each)
OPENALEX_MIN_CREDITS_FOR_SEARCH = 100

LAST_HEADERS: dict[str, dict] = {}
_last_call: dict[str, float] = {}


def today() -> str:
    return _dt.date.today().isoformat()


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# evidence notes
# --------------------------------------------------------------------------

def evidence(value, status: str, source: str | None = None, checked: str | None = None,
             location: str | None = None, quote: str | None = None, rationale: str | None = None) -> dict:
    """Build an evidence note. value/status/source always present; the rest only when given."""
    if status not in STATUSES:
        raise ValueError(f"unknown evidence status {status!r}; use one of {STATUSES}")
    e = {"value": value, "status": status, "source": source}
    if checked is not None:
        e["checked"] = checked
    if location is not None:
        e["location"] = location
    if quote is not None:
        e["quote"] = quote
    if rationale is not None:
        e["rationale"] = rationale
    return e


def is_evidence(x) -> bool:
    return isinstance(x, dict) and "status" in x and "value" in x


def check_evidence(field: str, e, errs: list) -> bool:
    """Append at most one error for this note; return True if clean."""
    if not is_evidence(e):
        errs.append(f"{field}: not an evidence note (needs value+status[+source])")
        return False
    st = e.get("status")
    if st not in STATUSES:
        errs.append(f"{field}: unknown status {st!r}")
    elif st in POSITIVE and e.get("value") is None:
        errs.append(f"{field}: status {st} but value is null (use a negative status instead)")
    elif st in POSITIVE and not e.get("source"):
        errs.append(f"{field}: status {st} requires a source URL/path")
    elif st == "inferred" and not e.get("rationale"):
        errs.append(f"{field}: inferred requires a rationale")
    elif st in NEGATIVE and e.get("value") is not None:
        errs.append(f"{field}: status {st} but a value is present (not found is not a value)")
    else:
        return True
    return False


# --------------------------------------------------------------------------
# ids / normalisation
# --------------------------------------------------------------------------

_ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_OLD_ARXIV_RE = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")


def norm_arxiv(s: str | None) -> str | None:
    if not s:
        return None
    m = _ARXIV_RE.search(s)
    if m:
        return m.group(1)
    m = _OLD_ARXIV_RE.search(s)
    return m.group(1) if m else None


def arxiv_version(s: str | None) -> str | None:
    if not s:
        return None
    m = _ARXIV_RE.search(s)
    return m.group(2) if m and m.group(2) else None


def norm_doi(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    return s or None


def norm_openalex(s: str | None) -> str | None:
    if not s:
        return None
    return s.rsplit("/", 1)[-1].upper()


def norm_title(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", s)


# --------------------------------------------------------------------------
# HTTP with cache, politeness, backoff
# --------------------------------------------------------------------------

def _resolve_ips(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, None)]


def _public_url(url) -> bool:
    """An http(s) URL whose host has only public addresses. URLs taken from paper texts, arXiv comments and OpenAlex
    locations are fetched only then: no localhost, LAN, link-local or file:// requests (security scan F10/F13)."""
    try:
        u = urllib.parse.urlsplit(str(url or ""))
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        try:
            ipaddress.ip_address(u.hostname)
            ips = [u.hostname]
        except ValueError:
            ips = _resolve_ips(u.hostname)
        return bool(ips) and all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips)
    except (OSError, ValueError, UnicodeError):
        return False


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    """A fetch marked `public_only` (a URL from a paper) may not redirect to a non-public host, so a public URL cannot
    bounce the request into the researcher's network; the mark goes on to the next hop. Other fetches are unchanged."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if getattr(req, "public_only", False) and not _public_url(newurl):
            raise urllib.error.HTTPError(newurl, code, f"redirect to a non-public address refused: {newurl}", headers, fp)
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is not None and getattr(req, "public_only", False):
            nxt.public_only = True
        return nxt


_OPENER = urllib.request.build_opener(_PublicRedirect())


def _urlopen(req, timeout=REQUEST_TIMEOUT):
    return _OPENER.open(req, timeout=timeout)


def _sleep(s: float):
    time.sleep(s)


def _cache_file(cache_dir: Path, url: str) -> Path:
    return Path(cache_dir) / "http" / (hashlib.sha256(url.encode()).hexdigest() + ".json")


# A spent OpenAlex daily budget answers 429 "Insufficient budget" with Retry-After = seconds to 00:00 UTC; retrying
# cannot clear it. Five capped retries cost ~5 min per call and ~10 min per snowball paper (agentic-systems review,
# 2026-10-13), so the host is skipped for the rest of the process. A burst 429 ("N requests per second",
# Retry-After 1) keeps the backoff.
HOST_DOWN_UNTIL: dict[str, float] = {}


def _openalex_budget_spent(body: str, retry_after: float | None) -> bool:
    return "insufficient budget" in (body or "").lower() or (retry_after is not None and retry_after > MAX_RETRY_AFTER)


def _mark_openalex_spent(headers, retry_after: float | None):
    now = time.time()
    midnight = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    until = now + retry_after if retry_after is not None else midnight.timestamp()
    first = "api.openalex.org" not in HOST_DOWN_UNTIL
    HOST_DOWN_UNTIL["api.openalex.org"] = until
    try:
        h = {k.lower(): v for k, v in dict(headers or {}).items()}
    except Exception:
        h = {}
    LAST_HEADERS["api.openalex.org"] = {**h, "x-ratelimit-remaining": "0"}   # title searches then skip themselves
    if first:
        whose = "this API key" if OPENALEX_KEY else "no API key: 1,000 credits a day, shared by everyone on this IP address"
        print(f"WARNING: the OpenAlex daily credit budget is used up ({whose}); it resets at 00:00 UTC, in "
              f"{(until - now) / 3600:.1f} h. OpenAlex calls are skipped until then; citation walks use Semantic "
              f"Scholar." + ("" if OPENALEX_KEY else " A free key from https://openalex.org/settings/api gives 10,000 "
                             "credits a day: set OPENALEX_API_KEY."), file=sys.stderr)


def openalex_spent() -> bool:
    return HOST_DOWN_UNTIL.get("api.openalex.org", 0) > time.time()


def http_get(url: str, cache_dir: Path, headers: dict | None = None, ttl: float | None = TTL_FOREVER,
             retries: int = MAX_RETRIES, public_only: bool = False) -> tuple[int, str]:
    """GET with on-disk cache. Returns (status, body). 4xx (except 429) is cached as a failure so
    reruns are idempotent; 429/5xx are retried with Retry-After or exponential backoff. `public_only` (a URL taken
    from a paper): a non-public host or redirect gives (0, "") and nothing is cached."""
    cf = _cache_file(cache_dir, url)
    if cf.exists():
        try:
            rec = json.loads(cf.read_text())
            age = time.time() - rec.get("fetched_at", 0)
            if ttl is None or age < ttl or CACHE_ONLY:
                return rec["status"], rec["body"]
        except (json.JSONDecodeError, KeyError):
            pass
    if CACHE_ONLY:
        return 0, ""   # `coverage --no-fetch`: a stale cache entry is served, a missing one is reported as a failure
    if public_only and not _public_url(url):
        return 0, ""
    host = urllib.parse.urlparse(url).netloc
    if HOST_DOWN_UNTIL.get(host, 0) > time.time():
        return 429, ""
    interval = HOST_INTERVAL.get(host, 0.0)
    hdrs = {"User-Agent": f"litrev/1.0 (+{MAILTO or 'mailto:unset'})", "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    status, body = 0, ""
    for attempt in range(retries):
        wait = max(0.0, _last_call.get(host, 0) + interval - time.time())
        if wait > 0:
            _sleep(wait)
        _last_call[host] = time.time()
        req = urllib.request.Request(url, headers=hdrs)
        req.public_only = public_only
        try:
            with _urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                status = getattr(r, "status", 200) or 200
                body = r.read().decode("utf-8", errors="replace")
                try:
                    LAST_HEADERS[host] = {k.lower(): v for k, v in dict(r.headers).items()}
                except Exception:
                    pass
            break
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            except Exception:
                body = ""
            if status == 429 or status >= 500:
                ra = None
                try:
                    ra = float(e.headers.get("Retry-After")) if e.headers and e.headers.get("Retry-After") is not None else None
                except (TypeError, ValueError):
                    ra = None
                if status == 429 and host == "api.openalex.org" and _openalex_budget_spent(body, ra):
                    _mark_openalex_spent(e.headers, ra)
                    break
                # cap Retry-After: a server-sent value of many minutes stalled a search run for 35+ min
                _sleep(min(ra, MAX_RETRY_AFTER) if ra is not None else float(2 ** attempt))
                continue
            break  # other 4xx: give up, cache the failure
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            # http.client.IncompleteRead (a truncated OpenAlex response) is an HTTPException, not an OSError
            status, body = 0, str(e)
            _sleep(float(2 ** attempt))
            continue
    cf.parent.mkdir(parents=True, exist_ok=True)
    if status != 429 and status != 0:   # the url without the api_key/mailto _oa() adds (security scan F6); same file name
        cf.write_text(json.dumps({"url": _drop_credentials(url), "fetched_at": time.time(), "status": status, "body": body}))
    return status, body


def http_json(url: str, cache_dir: Path, **kw):
    st, body = http_get(url, cache_dir, **kw)
    if st != 200:
        return st, None
    try:
        return st, json.loads(body)
    except json.JSONDecodeError:
        return st, None


# --------------------------------------------------------------------------
# parsers / classifiers (pure functions)
# --------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARX = "{http://arxiv.org/schemas/atom}"


def fetch_bytes(url: str, cache_dir: Path) -> tuple[int, bytes, str | None]:
    """Binary GET for figures, cached under cache/figures/ (successes and 4xx except 429). Returns (status, bytes, content_type).
    A figure URL comes from arXiv HTML (the author's img src): only public http(s) hosts, also after a redirect."""
    if not _public_url(url):
        return 0, b"", None
    key = hashlib.sha256(url.encode()).hexdigest()
    d = Path(cache_dir) / "figures"
    bf, mf = d / f"{key}.bin", d / f"{key}.json"
    if mf.exists():
        meta = json.loads(mf.read_text())
        if meta.get("status", 200) != 200:
            return meta["status"], b"", None  # cached client error: the figure is gone, do not refetch
        if bf.exists():
            return 200, bf.read_bytes(), meta.get("content_type")
    host = urllib.parse.urlparse(url).netloc
    wait = max(0.0, _last_call.get(host, 0) + HOST_INTERVAL.get(host, 0.0) - time.time())
    if wait > 0:
        _sleep(wait)
    _last_call[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": f"litrev/1.0 (+{MAILTO or 'mailto:unset'})"})
    req.public_only = True
    try:
        with _urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") if r.headers else None) or mimetypes.guess_type(url)[0]
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500 and e.code != 429:
            d.mkdir(parents=True, exist_ok=True)
            mf.write_text(json.dumps({"url": url, "status": e.code, "fetched_at": time.time()}))
        return e.code, b"", None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b"", None
    ctype = (ctype or "application/octet-stream").split(";")[0].strip()
    d.mkdir(parents=True, exist_ok=True)
    bf.write_bytes(data)
    mf.write_text(json.dumps({"url": url, "content_type": ctype, "fetched_at": time.time()}))
    return 200, data, ctype


def parse_arxiv_atom(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for ent in root.findall(_ATOM + "entry"):
        raw_id = (ent.findtext(_ATOM + "id") or "").strip()
        aid = norm_arxiv(raw_id)
        if not aid:
            continue
        title = re.sub(r"\s+", " ", ent.findtext(_ATOM + "title") or "").strip()
        abstract = re.sub(r"\s+", " ", ent.findtext(_ATOM + "summary") or "").strip()
        published = (ent.findtext(_ATOM + "published") or "")[:10]
        updated = (ent.findtext(_ATOM + "updated") or "")[:10]
        authors = [a.findtext(_ATOM + "name").strip() for a in ent.findall(_ATOM + "author") if a.findtext(_ATOM + "name")]
        cats = [c.get("term") for c in ent.findall(_ATOM + "category") if c.get("term")]
        prim = ent.find(_ARX + "primary_category")
        if prim is not None and prim.get("term") and prim.get("term") not in cats:
            cats.insert(0, prim.get("term"))
        comment = re.sub(r"\s+", " ", ent.findtext(_ARX + "comment") or "").strip()
        journal = (ent.findtext(_ARX + "journal_ref") or "").strip()
        doi = (ent.findtext(_ARX + "doi") or "").strip() or None
        pdf = None
        for l in ent.findall(_ATOM + "link"):
            if l.get("title") == "pdf":
                pdf = l.get("href")
        out.append({
            "arxiv_id": aid, "version": arxiv_version(raw_id), "title": title, "authors": authors,
            "year": int(published[:4]) if published[:4].isdigit() else None, "published": published, "updated": updated,
            "categories": cats, "abstract": abstract, "comment": comment, "journal_ref": journal, "doi": norm_doi(doi),
            "abs_url": f"https://arxiv.org/abs/{aid}", "pdf_url": pdf,
        })
    return out


def openalex_pick_best(results: list[dict], title: str) -> dict | None:
    """Among OpenAlex works whose title matches exactly (normalised), return the most-cited."""
    want = norm_title(title)
    cands = [r for r in results or [] if norm_title(r.get("title") or r.get("display_name")) == want]
    if not cands:
        return None
    return max(cands, key=lambda r: r.get("cited_by_count") or 0)


def openalex_institutions(work: dict) -> list[str]:
    out = []
    for a in work.get("authorships") or []:
        for inst in a.get("institutions") or []:
            n = inst.get("display_name")
            if n and n not in out:
                out.append(n)
        for aff in a.get("affiliations") or []:
            n = aff.get("raw_affiliation_string")
            if n and n not in out and not any(i.get("display_name") for i in a.get("institutions") or []):
                out.append(n)
    return out


def openalex_abstract(work: dict) -> str | None:
    inv = work.get("abstract_inverted_index")
    if not inv:
        return None
    pos = {}
    for w, idxs in inv.items():
        for i in idxs:
            pos[i] = w
    return " ".join(pos[i] for i in sorted(pos))


def openalex_arxiv_id(work: dict) -> str | None:
    doi = norm_doi((work.get("ids") or {}).get("doi") or work.get("doi"))
    if doi and doi.startswith("10.48550/arxiv."):
        return norm_arxiv(doi)
    for loc in [work.get("primary_location") or {}] + list(work.get("locations") or []):
        u = (loc or {}).get("landing_page_url") or (loc or {}).get("pdf_url") or ""
        if "arxiv.org" in u:
            a = norm_arxiv(u)
            if a:
                return a
    return None


# --------------------------------------------------------------------------
# published venue (peer-reviewed home of a paper; preprint servers do not count)
# --------------------------------------------------------------------------

_VENUE_SHORT = [  # (pattern on a long venue name, short name); first match wins
    (r"computer vision and pattern recognition|\bcvpr\b", "CVPR"), (r"international conference on computer vision|\biccv\b", "ICCV"),
    (r"european conference on computer vision|\beccv\b", "ECCV"), (r"winter conference on applications of computer vision|\bwacv\b", "WACV"),
    (r"neural information processing systems|\bneurips\b|\bnips\b", "NeurIPS"), (r"learning representations|\biclr\b", "ICLR"),
    (r"international conference on machine learning|\bicml\b", "ICML"), (r"\baaai\b", "AAAI"),
    (r"pattern analysis and machine intelligence|\btpami\b", "TPAMI"), (r"international journal of computer vision|\bijcv\b", "IJCV"),
    (r"robotics and automation letters", "RA-L"), (r"international conference on robotics and automation|\bicra\b", "ICRA"),
    (r"intelligent robots and systems|\biros\b", "IROS"), (r"isprs journal", "ISPRS J"),
    (r"conference on multimedia|acm multimedia|\bacm ?mm\b", "ACM MM"), (r"lecture notes in computer science", "LNCS"),
]
_VENUE_ACRONYMS = (r"CVPR|ICCV|ECCV|WACV|BMVC|ACCV|3DV|NeurIPS|NIPS|ICLR|ICML|AAAI|IJCAI|AISTATS|UAI|CoRL|ICRA|IROS|RA-?L|T-?RO|"
                   r"TPAMI|IJCV|TIP|TCSVT|TGRS|ISPRS|ACL|EMNLP|NAACL|EACL|COLING|TACL|SIGIR|CIKM|WSDM|KDD|WWW|TheWebConf|ICTIR|"
                   r"ECIR|TOIS|TKDE|RecSys|ACM ?MM|MICCAI|ICASSP|Interspeech|ITSC|TITS|IEEE ?VR")
_NOT_YET = re.compile(r"\b(submitted|under (review|submission)|in (review|submission)|preprint)\b", re.I)
_EXTENDS = re.compile(r"\b(extension|extended version|extends|journal version) of\b|\bbased on our\b|\bbuilds? on\b", re.I)
_ACCEPTED = re.compile(r"\b(accepted|published|to appear|appears?|appearing|presented)\b", re.I)
_PREPRINT_SOURCES = re.compile(r"arxiv|qeios|ssrn|research square|techrxiv|preprints\.org|biorxiv|medrxiv|openreview", re.I)
_VENUE_KINDS = {"journal": "journal", "conference": "conference", "book series": "proceedings", "ebook platform": "book"}


def _venue_year(text: str | None) -> int | None:
    m = re.findall(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)", text or "")
    if m:
        return int(m[-1])
    m = re.search(r"['’](\d{2})\b", text or "")
    return 2000 + int(m.group(1)) if m else None


def venue_short(name: str | None) -> str | None:
    """'CVPR' for 'IEEE/CVF Conference on Computer Vision and Pattern Recognition', the parenthetical acronym of
    'International ACM SIGIR Conference ... (SIGIR)', else None."""
    if not name:
        return None
    name = re.sub(r"([A-Za-z])[-'’]?(\d{2,4})\b", r"\1 \2", name)   # "ICRA2023", "CVPR-25" -> "ICRA 2023"
    for pat, short in _VENUE_SHORT:
        if re.search(pat, name, re.I):
            return short
    m = re.search(r"\(([A-Z][A-Za-z\-]{1,9})\)", name)
    if m:
        return m.group(1)
    m = re.search(r"\b(" + _VENUE_ACRONYMS + r")\b", name)
    return m.group(1) if m else None


def venue_from_comment(comment: str | None, journal_ref: str | None = None) -> dict | None:
    """Venue the authors name on arXiv: the journal_ref, or an acceptance phrase / 'CVPR 2024' in the comment.
    'Submitted to' / 'under review' never counts. Returns {name, short, year, kind, quote} or None."""
    if journal_ref and journal_ref.strip():
        j = journal_ref.strip()
        return {"name": j, "short": venue_short(j), "year": _venue_year(j),
                "kind": "workshop" if re.search(r"workshop", j, re.I) else None, "quote": j, "field": "journal_ref"}
    c = (comment or "").strip()
    if not c:
        return None
    for part in re.split(r"(?<=[.;])\s+|\s*\|\s*", c):
        if (_NOT_YET.search(part) and not _ACCEPTED.search(part)) or _EXTENDS.search(part):
            continue   # "Extension of ICRA 2023 paper" names an earlier paper's venue, not this one's
        m = re.search(r"\b(" + _VENUE_ACRONYMS + r")(?:\s*[-'’]?\s*(\d{4}|\d{2}))?(?!\w)", part)
        acc = _ACCEPTED.search(part)
        if m and (m.group(2) or acc):
            name = m.group(0).strip()
            yy = m.group(2)
            year = int(yy) if yy and len(yy) == 4 else 2000 + int(yy) if yy else _venue_year(part)
        elif acc:
            mm = re.search(r"\b(?:at|in|to|by|for)\s+(?:the\s+)?(.{4,90}?)(?:\s*[,.;()\[\]]|$)", part[acc.end():])
            if not mm or not (re.search(r"conference|journal|workshop|symposium|transactions|proceedings|letters", mm.group(1), re.I)
                              or venue_short(mm.group(1))):
                continue
            name, year = mm.group(1).strip(), _venue_year(part)
        else:
            continue
        return {"name": name, "short": venue_short(name), "year": year,
                "kind": "workshop" if re.search(r"workshop", part, re.I) else None, "quote": part.strip(), "field": "comment"}
    return None


def openalex_published_location(work: dict) -> dict | None:
    """The first peer-reviewed location of an OpenAlex work: a journal / conference / proceedings source that is the
    published version (preprint servers and repositories never count). Returns {name, kind, year, url} or None."""
    for loc in [work.get("primary_location") or {}] + list(work.get("locations") or []):
        src = (loc or {}).get("source") or {}
        name, kind = src.get("display_name"), _VENUE_KINDS.get(src.get("type") or "")
        if not name or not kind or _PREPRINT_SOURCES.search(name):
            continue
        if loc.get("is_published") is False or loc.get("version") not in (None, "publishedVersion"):
            continue
        return {"name": name, "kind": kind, "year": work.get("publication_year"),
                "url": loc.get("landing_page_url") or f"https://openalex.org/{norm_openalex(work.get('id'))}"}
    return None


def _shares_author(work: dict, authors: list[str]) -> bool:
    if not authors:
        return True
    mine = {norm_name(a.split()[-1]) for a in authors if a and a.split()}
    theirs = {norm_name((a.get("author") or {}).get("display_name", "").split()[-1])
              for a in work.get("authorships") or [] if (a.get("author") or {}).get("display_name", "").split()}
    return bool(mine & theirs)


def derive_published_venue(p: dict, works: list[dict] | None = None, resolved: bool = False) -> dict | None:
    """Evidence note for `published_venue`: verified from an OpenAlex published location (title matches must share an
    author), author-reported from the arXiv journal_ref / comment, not-found when the paper was resolved and is
    preprint-only, None (unknown yet) otherwise. Value: {name, short, year, kind}."""
    cm = venue_from_comment(p.get("comment"), p.get("journal_ref"))
    for w in works or []:
        if not w or not _shares_author(w, p.get("authors") or []):
            continue
        loc = openalex_published_location(w)
        if not loc:
            continue
        short = (cm or {}).get("short") or venue_short(loc["name"])   # "ECCV 2024" in the comment beats "LNCS"
        year = loc["year"] or (cm or {}).get("year") or p.get("year")
        kind = "workshop" if (cm or {}).get("kind") == "workshop" else loc["kind"]
        return evidence({"name": loc["name"], "short": short, "year": year, "kind": kind}, "verified",
                        source=f"https://api.openalex.org/works/{norm_openalex(w.get('id'))}", checked=today(),
                        location="locations[].source (published version)", rationale=f"published location: {loc['url']}")
    if cm:
        ax = (p.get("ids") or {}).get("arxiv")
        return evidence({"name": cm["name"], "short": cm["short"], "year": cm["year"] or p.get("year"), "kind": cm["kind"]},
                        "author-reported", source=f"https://arxiv.org/abs/{ax}" if ax else p.get("url"), checked=today(),
                        location=f"arXiv {cm['field']}", quote=cm["quote"])
    if resolved:
        return evidence(None, "not-found", checked=today(),
                        rationale="no peer-reviewed location in OpenAlex and no venue in the arXiv comment / journal_ref (preprint only)")
    return None


def published_venue_label(p: dict) -> str | None:
    """'CVPR 2024' / 'ACM Transactions on Information Systems 2025'; None for a preprint or unknown."""
    v = p.get("published_venue")
    if not (is_evidence(v) and v.get("status") in POSITIVE and isinstance(v.get("value"), dict)):
        return None
    val = v["value"]
    name = val.get("short") or val.get("name")
    return f"{name} {val['year']}" if val.get("year") and str(val["year"]) not in str(name) else name


def venue_text(p: dict) -> str:
    """'CVPR 2024', or 'no venue found, 2024' (not found is not a verified preprint: OpenAlex misses many CVF papers)."""
    lab = published_venue_label(p)
    if lab:
        return lab
    v = p.get("published_venue")
    head = "no venue found" if is_evidence(v) else "venue not looked up"
    return f"{head}, {p['year']}" if p.get("year") else head


def is_published(p: dict) -> bool:
    return published_venue_label(p) is not None


_GENERIC_OWNER_PARTS = {"research", "project", "projects", "group", "team", "official", "community", "public", "open",
                        "source", "code", "models", "model", "labs", "university", "institute", "github", "org"}


def _repo_owner_keys(code_url: str | None) -> set[str]:
    """The code repo's owner, whole and split into parts: an org's HF namespace is often one part of its GitHub owner
    (bigcode-project -> bigcode, Cornell-RelaxML -> relaxml). Generic parts ('research', 'project') never count."""
    m = re.search(r"github\.com/([^/#?\s]+)/", code_url or "")
    if not m:
        return set()
    parts = {norm_name(x) for x in re.split(r"[-_.]", m.group(1))}
    return {norm_name(m.group(1))} | {k for k in parts if len(k) >= 4 and k not in _GENERIC_OWNER_PARTS}


def classify_checkpoints(models: list[dict], authors: list[str], github_repo: str | None) -> dict:
    """Classify Hugging Face models that cite a paper. HF's filter=arxiv:<id> returns every model
    whose card cites the paper, so a hit is NOT proof of an official release. A model is
    'official' only if its namespace matches an author name or the paper's code-repo owner/name."""
    keys = set()
    for a in authors or []:
        k = norm_name(a)
        if k:
            keys.add(k)
        parts = a.split()
        if len(parts) >= 2:
            keys.add(norm_name(parts[-1] + parts[0]))
    if github_repo:
        m = re.search(r"github\.com/([^/]+)/([^/#?]+)", github_repo)
        if m:
            keys.add(norm_name(m.group(2)))
            keys |= _repo_owner_keys(github_repo)
    official, community = [], []
    for mdl in models or []:
        mid = mdl.get("id") or mdl.get("modelId") or ""
        ns = norm_name(mid.split("/")[0]) if "/" in mid else ""
        author = norm_name(mdl.get("author") or "")
        if ns and (ns in keys or author in keys):
            official.append(mid)
        else:
            community.append(mid)
    if official:
        return {"availability": "yes", "kind": "official", "official": official, "community": community}
    if community:
        return {"availability": "yes", "kind": "third-party", "official": [], "community": community}
    # Nothing found in the sources we searched; that is not a verified "no".
    return {"availability": "unknown", "kind": None, "official": [], "community": []}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "table", "figcaption", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag == "math":
            alt = dict(attrs).get("alttext")
            if alt and not self._skip:
                self.parts.append(" " + alt + " ")  # LaTeXML keeps the TeX source here; MathML children are skipped
            self._skip += 1
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if (tag in self.SKIP or tag == "math") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html_text: str) -> str:
    p = _TextExtractor()
    p.feed(html_text)
    txt = "".join(p.parts)
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


# TeX -> readable text. The data keeps the TeX the sources carry (LaTeXML alttext inside full texts, quotes and figure
# captions, with no $ around it; $...$ in arXiv / OpenAlex titles and abstracts): quotes are checked verbatim against
# the cached full text, and the extractor agents read TeX well. Every rendered output shows tex_to_text() of it.
_TEX_SYM = dict(zip("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi "
                    "varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi "
                    "Sigma Upsilon Phi Psi Omega".split(), "αβγδϵεζηθϑικλμνξπϖρϱσςτυϕφχψωΓΔΘΛΞΠΣΥΦΨΩ"))
_TEX_SYM.update({
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "forall": "∀",
    "exists": "∃", "neg": "¬", "lnot": "¬", "emptyset": "∅", "varnothing": "∅", "ell": "ℓ", "hbar": "ℏ", "star": "⋆",
    "ast": "∗", "circ": "∘", "bullet": "•", "dagger": "†", "ddagger": "‡", "prime": "′", "cdots": "⋯", "ldots": "…",
    "dots": "…", "vdots": "⋮", "ddots": "⋱", "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋",
    "lceil": "⌈", "rceil": "⌉", "top": "⊤", "bot": "⊥", "perp": "⊥", "uparrow": "↑", "downarrow": "↓",
    "Uparrow": "⇑", "Downarrow": "⇓", "checkmark": "✓", "triangle": "△", "square": "□", "angle": "∠",
    "degree": "°", "textdegree": "°", "lvert": "|", "rvert": "|", "vert": "|", "lVert": "‖", "rVert": "‖",
    "Vert": "‖", "backslash": "\\", "textbackslash": "\\", "textasciitilde": "~", "textasciicircum": "^",
    "textbar": "|", "textendash": "–", "textemdash": "—", "textbullet": "•", "textless": "<", "textgreater": ">",
    "texttrademark": "™", "textregistered": "®", "copyright": "©", "euro": "€", "pounds": "£", "o": "ø", "O": "Ø",
    "ss": "ß", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å", "l": "ł", "L": "Ł", "i": "ı",
    "quad": " ", "qquad": " ", "ie": "i.e.", "eg": "e.g.", "etc": "etc.", "vs": "vs.", "etal": "et al.",
    "SIUnitSymbolDegree": "°", "SIUnitSymbolCelsius": "°C", "blacktriangledown": "▼", "blacktriangleup": "▲",
    "triangledown": "▽", "bigstar": "★", "diamond": "⋄"})
_TEX_SPACED = {   # binary operators, relations and arrows: one space on each side
    "times": "×", "pm": "±", "mp": "∓", "cdot": "·", "div": "÷", "leq": "≤", "le": "≤", "leqslant": "≤",
    "geq": "≥", "ge": "≥", "geqslant": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "sim": "∼", "simeq": "≃",
    "cong": "≅", "equiv": "≡", "propto": "∝", "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "∖", "rightarrow": "→", "to": "→",
    "leftarrow": "←", "gets": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "Leftrightarrow": "⇔", "implies": "⟹", "iff": "⟺", "mapsto": "↦", "longrightarrow": "⟶", "gg": "≫",
    "ll": "≪", "otimes": "⊗", "oplus": "⊕", "odot": "⊙", "wedge": "∧", "vee": "∨", "land": "∧", "lor": "∨",
    "coloneqq": "≔", "triangleq": "≜", "prec": "≺", "succ": "≻", "preceq": "⪯", "succeq": "⪰", "mid": "∣",
    "parallel": "∥", "models": "⊨", "vdash": "⊢", "ominus": "⊖"}
_TEX_FUNC = set("arccos arcsin arctan arg argmax argmin cos cosh cot coth csc deg det dim exp gcd hom inf ker lg lim "
                "liminf limsup ln log max min Pr sec sgn sin sinh sup tan tanh".split())
_TEX_FONT = set("mathbf mathrm mathit mathsf mathtt mathnormal boldsymbol bm pmb operatorname mathop mathbin mathrel "
                "mathord underline ensuremath".split())          # argument shown as is (math)
_TEX_TEXT = set("text textbf textit textrm texttt textsf textsc textnormal textup textsl emph mbox hbox fbox url".split())
_TEX_ALPHA = {
    "mathcal": dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "𝒜ℬ𝒞𝒟ℰℱ𝒢ℋℐ𝒥𝒦ℒℳ𝒩𝒪𝒫𝒬ℛ𝒮𝒯𝒰𝒱𝒲𝒳𝒴𝒵")),
    "mathbb": dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ1", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ𝟙")),
    "mathfrak": {}}
_TEX_ALPHA["mathscr"] = _TEX_ALPHA["mathcal"]
_TEX_ACCENT = {"hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0305", "tilde": "\u0303",
               "widetilde": "\u0303", "vec": "\u20d7", "dot": "\u0307", "ddot": "\u0308", "check": "\u030c",
               "breve": "\u0306", "acute": "\u0301", "grave": "\u0300", "v": "\u030c", "u": "\u0306", "H": "\u030b",
               "c": "\u0327", "k": "\u0328", "r": "\u030a"}
_TEX_CTRL_ACCENT = {'"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303", "=": "\u0304", ".": "\u0307"}
_TEX_DROP = set("left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr displaystyle textstyle "
                "scriptstyle scriptscriptstyle limits nolimits nonumber notag not centering noindent hfill allowbreak "
                "rm bf it sf tt sl sc cal bfseries itshape mdseries upshape normalfont tiny scriptsize footnotesize small "
                "normalsize large Large LARGE huge Huge midrule toprule bottomrule hline penalty".split())
_TEX_DROP_ARG = set("label hspace vspace phantom hphantom vphantom color begin end".split())
_SUP = dict(zip("0123456789+-−=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾"))
_SUP.update(zip("abcdefghijklmnoprstuvwxyz", "ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"))
_SUP.update(zip("ABDEGHIJKLMNOPRTUVW", "ᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ"))
_SUP.update(zip("αβγδεθφχ∘⊤*∗", "ᵅᵝᵞᵟᵋᶿᵠᵡ°ᵀ**"))
_SUB = dict(zip("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎"))
_SUB.update(zip("aehijklmnoprstuvx", "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
_SUB.update(zip("βγρφχ", "ᵦᵧᵨᵩᵪ"))
_TEX_HINT = re.compile(r"\\[A-Za-z%&#$_{},;:!|\"'`^~=.(\[]|[_^]\{|\^\\|\$")
# $...$ as pandoc reads it (no space inside the dollars; a digit right after the closing $ ends nothing, so '$5 and $10'
# stays prose), except that a span holding TeX markup may have spaces inside and touch a digit: '$\sim$39K',
# '256$\times$256', 'Sat. $\rightarrow $ Dro.'
_TEX_MATH = re.compile(r"\$\$(.+?)\$\$|\\\((.+?)\\\)|\\\[(.+?)\\\]"
                       r"|(?<!\\)\$(?=[^$]*?[\\^_{])((?:[^$\\]|\\.)+?)\$"
                       r"|(?<!\\)\$(?=\S)((?:[^$\\]|\\.)+?)(?<=\S)\$(?!\d)", re.S)
# Crossref / Springer abstracts follow each inline formula's text with the same formula as a whole TeX document
_TEX_DOC = re.compile(r"\\documentclass.*?(?:\\end\{document\}|$)", re.S)
_TEX_WORD = re.compile(r"[\w.′*\u0300-\u036f]+")
_TEX_OP = "\x01"     # brackets a spaced operator until _tex_space_ops() places its spaces
_TEX_OVER = "\x02"   # {a \over b}: splits its group into a/b when the group closes


def _tex_close(s: str, i: int) -> int:
    """Index of the } that closes the { at s[i], or -1."""
    d, k = 0, i
    while k < len(s):
        c = s[k]
        if c == "\\":
            k += 2
            continue
        d += (c == "{") - (c == "}")
        if d == 0:
            return k
        k += 1
    return -1


def _tex_group_ok(s: str, i: int) -> bool:
    """Outside $...$, a {...} group is TeX (and is unwrapped) only when it is empty or holds TeX: prose braces stay."""
    j = _tex_close(s, i)
    body = s[i + 1:j] if j > 0 else None
    return body is not None and (body == "" or body[0] in "\\{" or bool(re.search(r"\\[A-Za-z]|[_^]\{", body)))


def _tex_space_ops(t: str) -> str:
    parts = t.split(_TEX_OP)
    out = parts[0]
    for k in range(1, len(parts) - 1, 2):
        out, rest = out.rstrip(" "), parts[k + 1].lstrip(" ")
        lead = " " if out and out[-1] not in "([{\n" else ""
        trail = " " if rest and rest[0] not in ")]},.;:\n" else ""
        out += lead + parts[k] + trail + rest
    return out


def _tex_wrap(t: str) -> str:
    t = _tex_space_ops(t).strip()
    return t if _TEX_WORD.fullmatch(t) or (t[:1] == "(" and t[-1:] == ")") else f"({t})"


def _tex_script(inner: str, sup: bool) -> str:
    """x^{2} -> x², t_{1} -> t₁; long or unmappable scripts -> R_srch, p^(q,I)."""
    t = _tex_space_ops(inner).strip()
    if not t or set(t) <= {"′"}:
        return t
    m = _SUP if sup else _SUB
    if all(c in m for c in t) and (len(t) <= 2 or re.fullmatch(r"[0-9+\-−=()]+", t)):
        return "".join(m[c] for c in t)
    return ("^" if sup else "_") + (t if _TEX_WORD.fullmatch(t) else _tex_wrap(t))


def _tex_arg(s: str, i: int, math: bool) -> tuple[str, int]:
    """One macro argument at s[i]: a {group}, a control sequence or a single character."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "{":
        return _tex_run(s, i + 1, math, close=True)
    if s[i] == "\\":
        out: list[str] = []
        i = _tex_cmd(s, i, True, out)
        return "".join(out), i
    return s[i], i + 1


def _tex_cmd(s: str, i: int, math: bool, out: list) -> int:
    """Convert the control sequence at s[i] (a backslash) into `out`; return the index after it."""
    n, j = len(s), i + 1
    if j >= n:
        out.append("\\")
        return n
    c = s[j]
    if not c.isalpha():
        m = re.match(r"\{([A-Za-z])\}|([A-Za-z])", s[j + 1:]) if c in _TEX_CTRL_ACCENT else None
        if m:                                           # text accents: Schr\"odinger, {\'e}
            out.append(unicodedata.normalize("NFC", (m.group(1) or m.group(2)) + _TEX_CTRL_ACCENT[c]))
            return j + 1 + m.end()
        rep = {",": " ", ":": " ", ";": " ", ">": " ", " ": " ", "!": "", "{": "{", "}": "}", "%": "%", "&": "&",
               "#": "#", "$": "$", "_": "_", **({"|": "‖", "\\": "; "} if math else {})}
        out.append(rep.get(c, "\\" + c))
        return j + 1
    k = j
    while k < n and s[k].isalpha():
        k += 1
    name = s[j:k]
    if name == "operatorname" and s[k:k + 1] == "*":
        k += 1
    known = (name in _TEX_SYM or name in _TEX_SPACED or name in _TEX_FUNC or name in _TEX_FONT or name in _TEX_TEXT
             or name in _TEX_ALPHA or name in _TEX_ACCENT or name in _TEX_DROP or name in _TEX_DROP_ARG
             or name in ("frac", "dfrac", "tfrac", "cfrac", "sqrt", "binom", "textcolor", "colorbox", "tag", "over"))
    if not known:
        if s[k:k + 1] == "{":                           # \mathds{1}, \textsl{x}: show the argument
            arg, k = _tex_arg(s, k, math)
            out.append(arg)
        else:
            out.append(s[i:k])                          # anything else stays as written
        return k
    if math:                                            # TeX drops the spaces after a control word
        while k < n and s[k] == " ":
            k += 1
    if name in _TEX_SPACED:
        out.append(_TEX_OP + _TEX_SPACED[name] + _TEX_OP)
    elif name in _TEX_SYM:
        out.append(_TEX_SYM[name])
    elif name in _TEX_FUNC:
        out.append(name)
        if s[k:k + 1] == "{":
            arg, k = _tex_arg(s, k, True)
            out.append(" " + arg)
        elif s[k:k + 1].isalnum():
            out.append(" ")
    elif name in _TEX_FONT or name in _TEX_TEXT:
        arg, k = _tex_arg(s, k, name in _TEX_FONT)
        out.append(arg)
    elif name in _TEX_ALPHA:
        arg, k = _tex_arg(s, k, True)
        out.append("".join(_TEX_ALPHA[name].get(ch, ch) for ch in arg))
    elif name in _TEX_ACCENT:
        arg, k = _tex_arg(s, k, math)
        out.append(unicodedata.normalize("NFC", "".join(ch + _TEX_ACCENT[name] if ch.isalnum() else ch for ch in arg.strip())))
    elif name in ("frac", "dfrac", "tfrac", "cfrac", "binom"):
        a, k = _tex_arg(s, k, True)
        b, k = _tex_arg(s, k, True)
        out.append(f"C({_tex_space_ops(a)},{_tex_space_ops(b)})" if name == "binom" else f"{_tex_wrap(a)}/{_tex_wrap(b)}")
    elif name == "sqrt":
        root = ""
        if s[k:k + 1] == "[" and "]" in s[k:]:
            root, k = s[k + 1:s.index("]", k)].strip(), s.index("]", k) + 1
        arg, k = _tex_arg(s, k, True)
        out.append({"": "√", "3": "∛", "4": "∜"}.get(root, _tex_script(root, True) + "√") + _tex_wrap(arg))
    elif name in ("textcolor", "colorbox"):
        _, k = _tex_arg(s, k, False)
        arg, k = _tex_arg(s, k, False)
        out.append(arg)
    elif name in _TEX_DROP_ARG or name == "tag":
        _, k = _tex_arg(s, k, False)
    elif name == "over":                                # outside $...$ the group is unknown: a plain slash
        out.append(_TEX_OVER if math else "/")
    elif name in ("left", "right", "middle") or name.startswith(("big", "Big")):
        if s[k:k + 1] == ".":                           # \left. is an empty delimiter
            k += 1
    elif name == "penalty":
        k = re.match(r" *-?\d*", s[k:]).end() + k      # \penalty10000
    elif name in _TEX_DROP and s[k:k + 1] == " " and (not out or out[-1][-1:] in ("", " ", "\n")):
        k += 1                                          # 'introduces \rm CALDERA': one space, not two
    return k


def _tex_run(s: str, i: int, math: bool, close: bool = False) -> tuple[str, int]:
    """Convert s[i:] up to the end, or with close=True up to the } that closes the current group."""
    out: list[str] = []
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "}":
            if depth:                                   # closes a prose brace
                depth -= 1
                out.append(c)
            elif close:
                return _tex_over("".join(out)), i + 1
            elif not math:
                out.append(c)
            i += 1
        elif c == "\\":
            i = _tex_cmd(s, i, math, out)
        elif c in "_^" and (math or (s[i + 1:i + 2] == "\\" and s[i + 2:i + 3].isalpha())
                            or (s[i + 1:i + 2] == "{" and _tex_close(s, i + 1) > 0)):
            arg, i = _tex_arg(s, i + 1, True)           # outside $...$ only braced scripts: snake_case stays
            out.append(_tex_script(arg, c == "^"))
        elif c == "{":
            if math or _tex_group_ok(s, i):
                inner, i = _tex_run(s, i + 1, True, close=True)
                out.append(inner)
            else:
                out.append(c)
                depth += 1
                i += 1
        elif c == "~" and (math or s[i + 1:i + 2] == "\\"):
            out.append(" ")
            i += 1
        elif math and c in "&'":
            out.append(" " if c == "&" else "′")
            i += 1
        else:
            out.append(c)
            i += 1
    return _tex_over("".join(out)), i


def _tex_over(t: str) -> str:
    if _TEX_OVER not in t:
        return t
    a, b = t.split(_TEX_OVER, 1)
    return f"{_tex_wrap(a)}/{_tex_wrap(b.replace(_TEX_OVER, '/'))}"


def tex_to_text(s):
    """Readable Unicode text for TeX in a string: 'R_{srch}' -> 'R_srch', '30^{\\circ}' -> '30°',
    '$π^3$: ...' -> 'π³: ...', '10\\times 10' -> '10 × 10'. Prose without TeX comes back unchanged
    (snake_case, '$5 and $10', unknown macros), and converting twice gives the same text."""
    if not isinstance(s, str) or not _TEX_HINT.search(s):
        return s
    raw, s = s, _TEX_DOC.sub("", s)
    out, k = [], 0
    try:
        for m in _TEX_MATH.finditer(s):
            out.append(_tex_run(s[k:m.start()], 0, False)[0])
            # spaced inside its own span only: '$\sim$39K' -> '∼39K' (TeX puts no space where an operand is missing)
            out.append(_tex_space_ops(_tex_run(next(g for g in m.groups() if g is not None), 0, True)[0]))
            k = m.end()
        out.append(_tex_run(s[k:], 0, False)[0])
    except RecursionError:      # ~1000 nested braces in an outside abstract stopped every render (security scan F9)
        return raw
    return _tex_space_ops("".join(out))


def tex_display(obj):
    """A copy of `obj` (dicts, lists, strings) with tex_to_text() on every string value; keys, URLs and data: URIs
    stay as they are."""
    if isinstance(obj, str):
        return obj if obj.startswith(("http://", "https://", "data:")) else tex_to_text(obj)
    if isinstance(obj, dict):
        return {k: tex_display(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [tex_display(v) for v in obj]
    return obj


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def quote_in_text(quote: str | None, text: str) -> bool:
    if not quote:
        return False
    return _ws(quote) in _ws(text)


_PLACEHOLDER = re.compile(r"^\s*(n/?a\b.*|none|null|unknown|not (stated|specified|reported|given)\b.*|unspecified|-+|—)\s*$", re.I)


def is_placeholder_name(name: str | None) -> bool:
    """Extractor filler such as 'not stated' or 'n/a (model-level)', or a junk name that is mostly symbols (a
    pwc-archive board called '^(#$!@#$)(()))******'): never a dataset or metric name."""
    n = str(name or "")
    return not n or bool(_PLACEHOLDER.match(n)) or len(re.findall(r"[A-Za-z0-9]", n)) < max(2, len(n) // 2)


# Repos that papers cite but do not own (backbones, datasets, toolkits). A printed URL to one of these is not the paper's code.
THIRD_PARTY_REPO_OWNERS = {
    "facebookresearch", "pytorch", "huggingface", "openai", "google", "google-research", "tensorflow", "microsoft", "nvlabs",
    "open-mmlab", "lucidrains", "rwightman", "huggingface", "compvis", "stability-ai", "isl-org", "depth-anything", "liheyoung",
    "cvg", "magicleap", "zju3dv", "naver", "colmap", "graphdeco-inria", "nerfstudio-project", "ultralytics", "jeff-zilence",
    "liumouliu", "mubarizzaffar", "nianticlabs", "skyy93", "qgis",
}


def repo_urls_in_text(text: str) -> list[str]:
    """GitHub/GitLab repo URLs printed in a paper, in order, without trailing punctuation or known third-party owners."""
    out = []
    for m in re.finditer(r"https?://(?:www\.)?(github\.com|gitlab\.com)/([\w.\-]+)/([\w.\-]+)", text or ""):
        owner, repo = m.group(2), m.group(3).rstrip(".")
        repo = re.sub(r"\.git$", "", repo)
        if owner.lower() in THIRD_PARTY_REPO_OWNERS or not repo:
            continue
        u = f"https://{m.group(1)}/{owner}/{repo}"
        if u not in out:
            out.append(u)
    return out


# Repos printed by a LaTeX template (\acmCodeLink defaults to the acmart repo) or by tools/libraries a paper only uses.
TEMPLATE_REPOS = {"borisveytsman/acmart", "nerfies/nerfies.github.io", "eliahuhorwitz/academic-project-page-template"}  # LaTeX / project-page templates
TOOL_REPO_OWNERS = {"terrierteam", "stanfordfuturedata", "castorini", "texttron", "vllmproject", "metallama", "scikitlearn",
                    "explosion", "langchainai", "runllama", "beircellar", "ukplab"}
_REPO_URL = re.compile(r"https?://(?:www\.)?(github\.com|gitlab\.com)/([\w.\-]+)/([\w.\-]+)")
# the sentence leading up to a URL says the authors release it ...
_OWN_CODE = re.compile(
    r"\b(we|our)\b[^.]{0,60}\b(release[sd]?|releasing|open[- ]?sourced?|make|made|provide[sd]?|publish(ed)?|share[sd]?)\b[^.]{0,60}"
    r"\b(code|implementation|source|repository|data|benchmark|artifacts?|resources|scripts|package)\b"
    r"|\bour (code|implementation|source code|repository|github|codebase|package)\b"
    r"|\b(code|implementation|source code|codebase|repository)\b[^.]{0,80}\b(is|are|will be|has been)\s+(publicly\s+|freely\s+|now\s+|made\s+)*"
    r"(available|released|accessible|provided)\b"
    r"|\b(code|source code|implementation|code repository)\b[^.]{0,40}\bcan be found\b"
    r"|\bwill (be )?release\b|\bmade (publicly )?available\b"
    r"|\b(code|project page|github)\s*[:—–]\s*$", re.I)
# ... or merely that the paper used it (outweighs a title-token match)
_USED_CODE = re.compile(
    r"\b(we|they|baselines?|methods?)\s+(use[sd]?|utili[sz]e[sd]?|employ(ed|s)?|adopt(ed|s)?|leverage[sd]?|obtain(ed)?|"
    r"implement(ed)? based|build on|built on|rely on|relied on)\b"
    r"|\bimplementations? (provided|released) (at|by)\b|\breleased by the authors?\b|\bprovided by the authors?\b"
    r"|\bauthors'? (public|official|released) code\b|\bpublicly available (code|implementation|resource|toolkit)s? (for|from)\b"
    r"|\bbased on\b|\bbuilt (up)?on\b|\bfollowing\b|\bresources? from\b|\bcome from\b|\bused\b|\busing\b|\bintegrations?\b",
    re.I)
_AVAILABLE_AT = re.compile(r"\b(available|accessible) (at|on|from|in)\b|\bgithub repository\b", re.I)
_STOP = {"with", "from", "for", "and", "the", "towards", "toward", "via", "using", "into", "beyond", "when", "what", "does", "your", "their",
         "model", "models", "learning", "large", "language", "based", "study", "analysis", "approach", "framework", "method", "methods"}


def _repo_sentence(text: str, start: int) -> tuple[str, bool]:
    """The sentence that leads up to a URL at `start`, with trailing footnote markers removed; and whether a LaTeX
    macro (\\acmCodeLink...) sits immediately before it."""
    w = text[max(0, start - 300):start]
    macro = bool(re.search(r"\\[A-Za-z]+\s*$", w))
    w = re.sub(r"[\s\d*†‡§¶]+$", "", w)          # 'repository. 33 3 ' -> 'repository.'
    w = re.sub(r"[.;]+$", "", w)
    cut = [m.end() for m in re.finditer(r"[.!?]\s+(?=[A-Z])|\n\s*\n", w)]   # sentence end or paragraph/footnote break
    return re.sub(r"\s+", " ", w[cut[-1]:] if cut else w).strip(), macro


def _title_tokens(title: str | None) -> set[str]:
    return {t for t in norm_title(title).split() if (len(t) >= 4 or any(c.isdigit() for c in t)) and t not in _STOP}


def _author_tokens(authors) -> set[str]:
    return {t for a in authors or [] for t in norm_title(a).split() if len(t) >= 4}


def score_repo_candidates(text: str, title: str | None = None, authors=None) -> list[dict]:
    """Score every GitHub/GitLab repo printed in a paper as the paper's own code. Signals: the sentence before the URL
    says the authors release it (+3) or only that they used it (-4); 'available at' (+2); position on the first page
    (+2); owner matches an author name (+2); repo name equals a title word (+3) or overlaps one (+1); tool/library
    owner (-4). Template repos and URLs right after a LaTeX macro are dropped. Best first; ties go to the earliest."""
    text = text or ""
    ttoks, atoks = _title_tokens(title), _author_tokens(authors)
    first_page = min(6000, max(2000, len(text) // 8))
    found: dict[str, dict] = {}
    for m in _REPO_URL.finditer(text):
        owner, repo = m.group(2), re.sub(r"\.git$", "", m.group(3).rstrip("."))
        key = f"{owner}/{repo}".lower()
        if not repo or key in TEMPLATE_REPOS:
            continue
        sent, macro = _repo_sentence(text, m.start())
        if macro:
            continue
        s, why = 0, []
        own, used = _OWN_CODE.search(sent), _USED_CODE.search(sent)
        if own:
            s += 3; why.append(f"release phrase {own.group(0)[:40]!r}")
        elif _AVAILABLE_AT.search(sent):
            s += 2; why.append("'available at'")
        if used:
            s -= 4; why.append(f"use phrase {used.group(0)[:30]!r}")
        if m.start() < first_page:
            s += 2; why.append("first page")
        c = found.get(key)
        if c is None or s > c["context_score"]:
            q = re.sub(r"\s+", " ", text[max(0, m.start() - 120):m.end() + 10]).strip()
            found[key] = {"url": f"https://{m.group(1)}/{owner}/{repo}", "context_score": s, "context": why, "quote": q,
                          "pos": c["pos"] if c else m.start(), "owner": owner, "repo": repo}
    out = []
    for c in found.values():
        s, why = c["context_score"], list(c["context"])
        o = norm_name(c["owner"])
        if o in TOOL_REPO_OWNERS or c["owner"].lower() in THIRD_PARTY_REPO_OWNERS:
            s -= 4; why.append("tool/library owner")
        if any(t in o for t in atoks):
            s += 2; why.append("owner matches an author")
        rn = norm_name(c["repo"])
        rtoks = {norm_name(t) for t in re.split(r"[-_.]+", c["repo"]) if len(t) >= 3}
        short = norm_name(re.sub(r"\$[^$]*\$", lambda m: re.sub(r"[^0-9A-Za-z]", "", m.group(0)), (title or "").split(":")[0])) if ":" in (title or "") else ""
        if rn in ttoks or (short and rn == short):
            s += 3; why.append("repo name is a title word" if rn in ttoks else "repo name is the paper's short name")
        elif rtoks & ttoks or any(len(t) >= 5 and t in rn for t in ttoks):
            s += 1; why.append("repo name overlaps the title")
        out.append({"url": c["url"], "score": s, "why": why, "quote": c["quote"], "pos": c["pos"]})
    out.sort(key=lambda c: (-c["score"], c["pos"]))
    return out


CODE_REPO_MIN_SCORE = 3
_CODE_LINK_TEXT = re.compile(r"\b(code|github|source|implementation)\b", re.I)
_NOT_PROJECT_PAGE = re.compile(r"(github\.com|gitlab\.com|arxiv\.org|doi\.org|drive\.google|youtu|huggingface\.co|dropbox|openreview)", re.I)


_PAGE_INTRO = re.compile(r"\b(project|code|demo)?\s*(page|website|webpage|homepage|site)\b\s*(is|at|:)?[^.\n]{0,40}$", re.I)


def project_page_urls(p: dict, text: str | None = None) -> list[str]:
    """Project pages to look for the paper's repo on: every non-repo URL in the arXiv comment, then URLs on the paper's
    first page that the text introduces as a project page/website ('Project page: https://...', MoGe-3, DepthMaster).
    A bare URL with no such intro (an author homepage, a publisher logo) is not followed."""
    out = []
    for u in re.findall(r"https?://[^\s,;)]+", p.get("comment") or ""):
        out.append(u.rstrip("."))
    t = text or ""
    first = t[:min(6000, max(2000, len(t) // 8))]
    for m in re.finditer(r"https?://[^\s,;)\]}]+", first):
        if _PAGE_INTRO.search(first[max(0, m.start() - 60):m.start()]):
            out.append(m.group(0).rstrip("."))
    return [u for u in dict.fromkeys(out) if not _NOT_PROJECT_PAGE.search(u)]


def project_page_repos(rv: "Review", p: dict, text: str | None = None) -> list[dict]:
    """Repos linked from the paper's project page(s) (project_page_urls): MoGe-2's comment names only its page, which
    links github.com/microsoft/moge. Each link becomes '<link text> <url>' and is scored with score_repo_candidates;
    a link whose text says Code/GitHub gets +3 (the page is the authors' own)."""
    out = []
    for u in project_page_urls(p, text):
        st, html = http_get(u, rv.cache_dir, ttl=TTL_SEARCH, public_only=True)   # the author wrote this URL
        if st != 200 or not html:
            continue
        lines, code_text = [], set()
        for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.I | re.S):
            label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
            lines.append(f"{label} {m.group(1)}")
            if _CODE_LINK_TEXT.search(label) and _REPO_URL.match(m.group(1)):
                code_text.add(m.group(1).lower().rstrip("/"))
        for c in score_repo_candidates(". ".join(lines), p.get("title"), p.get("authors")):
            if any(c["url"].lower() == k or k.startswith(c["url"].lower() + "/") for k in code_text):
                c["score"] += 3; c["why"].append("link text names it as the code")
            out.append({**c, "page": u})
    out.sort(key=lambda c: -c["score"])
    return out


def pick_code_repo(text: str, title: str | None = None, authors=None) -> dict | None:
    """The printed repo that is clearly the paper's own (score >= CODE_REPO_MIN_SCORE), else None (record not-found)."""
    c = score_repo_candidates(text, title, authors)
    return c[0] if c and c[0]["score"] >= CODE_REPO_MIN_SCORE else None


def plausible_third_party(link: dict, title: str | None) -> bool:
    """A non-official pwc-archive link counts only if the paper mentions it or its name overlaps the title; links found
    only because some repo mentions the paper (a reading list, an RSS aggregator) are not implementations of it."""
    if link.get("mentioned_in_paper"):
        return True
    m = re.search(r"github\.com/[^/]+/([^/#?]+)", link.get("repo_url") or "")
    if not m:
        return False
    rn = norm_name(m.group(1))
    return any(len(t) >= 4 and t in rn for t in _title_tokens(title))


_CKPT_WORDS = re.compile(r"(checkpoint|pre-?trained (model|weight)|trained models?|prequantized|quantized [\w#+-]+ models? (?:are|is) (?:available|released|provided)|model weights|weights\b|model zoo|\.pth\b|\.ckpt\b)", re.I)
_CKPT_HOST = re.compile(r"(drive\.google|1drv\.ms|onedrive|sharepoint|huggingface\.co/(?!datasets)|pan\.baidu|zenodo|surfdrive|dropbox|/releases)", re.I)


HF_CONVERSION_OWNERS = {"onnx-community", "xenova", "mlx-community", "thebloke", "unsloth", "lmstudio-community", "bartowski",
                        "gguf-org", "openvino", "qualcomm", "coreml-community", "timm"}


_NOT_WEIGHTS_URL = re.compile(r"/badge/|shields\.io|\.svg(?:$|[?#])|[\[\]{}<>]", re.I)


def _own_weights_link(url: str, title: str | None, authors=None, code_url: str | None = None) -> bool:
    """A Hugging Face link counts as the paper's own weights only if its owner matches an author, or its repo name holds
    the paper's short name, a title word or the code repo's name (CtrlA's README lists BAAI/bge-large-en-v1.5, the
    retriever it uses). Other hosts (Drive, releases, Zenodo) are not checked. Without a title nothing is filtered."""
    if _NOT_WEIGHTS_URL.search(url):
        return False          # a DOI badge image (P637's zenodo.org/badge/...svg) or a URL template ("opt-[MODEL-SIZE]")
    if title is None or "huggingface.co" not in url:
        return True
    if re.search(r"huggingface\.co/(spaces|docs|front|blog|papers)/", url):
        return False          # a demo or leaderboard Space, the docs, a logo asset, a paper page: never weights
    m = re.search(r"huggingface\.co/([^/?#\s]+)/([^/?#\s`]+)", url)
    owner, repo = (norm_name(m.group(1)), norm_name(m.group(2))) if m else ("", norm_name(url.split("huggingface.co", 1)[1]))
    if m and m.group(1).lower() in HF_CONVERSION_OWNERS:
        return False          # onnx-community/metric3d-vit-small: a format conversion, not the authors' release
    atoks = {t for t in _author_tokens(authors) if len(t) >= 5}   # 'chen' (Guanying Chen) is inside 'ruicheng
    short = norm_name(title.split(":")[0]) if ":" in title else ""
    cm = re.search(r"github\.com/[^/]+/([^/#?\s]+)", code_url or "")
    code = norm_name(cm.group(1)) if cm else ""
    # no plain title-word match: GenPercept ('...Repurposing Diffusion Models...') would take stabilityai/stable-diffusion-2-1
    om = re.search(r"github\.com/([^/#?\s]+)/", code_url or "")
    owner_parts = _repo_owner_keys(code_url) - {norm_name(om.group(1))} if om else set()   # QuIP#: relaxml of Cornell-RelaxML
    return bool((owner and any(t in owner for t in atoks)) or (short and short in repo) or (len(code) >= 4 and code in repo)
                or (owner and owner in owner_parts))


WEIGHTS_MIN_SCORE = 3
_HF_MODEL_URL = re.compile(r"(?:https?://)?(?:www\.)?huggingface\.co/(?!(?:datasets|spaces|docs|front|blog|papers|api|models|collections)/)"
                           r"([\w\-]+(?:\.[\w\-]+)*)/([\w\-]+(?:\.[\w\-]+)*)")
_OWN_WEIGHTS = re.compile(
    r"\b(we|our)\b[^.]{0,60}\b(release[sd]?|releasing|open[- ]?sourced?|make|made|provide[sd]?|publish(ed)?|share[sd]?|upload(ed)?)\b"
    r"[^.]{0,60}\b(models?|weights|checkpoints?|artifacts?)\b"
    r"|\bour (models?|weights|checkpoints?)\b[^.]{0,80}\b(is|are|will be|has been|have been)\s+(publicly\s+|freely\s+|now\s+|made\s+)*"
    r"(available|released|accessible|provided)\b", re.I)
_HF_COLLECTION_URL = re.compile(r"(?:https?://)?(?:www\.)?huggingface\.co/collections/([\w\-]+)/([\w\-]+)")
_FOUND_AT = re.compile(r"\b(can be (found|downloaded|accessed)|(is|are) (publicly |freely )?(released|available|hosted))\s+(in|at|on|from|via)\b", re.I)
_AVAIL_SECTION = re.compile(r"(^|\n)\s*(\d+(\.\d+)*\s+)?(code|data|model|software|resource)s?( and (code|data|models?|materials))? availability\b", re.I)
# LlaSMol: "We apply their released checkpoint" names a baseline's weights, not the authors'
_THEIR_RELEASE = re.compile(r"\b(their|its|the authors'?|the original|official)\s+(released|public|publicly released|open[- ]source[d]?|provided)\b"
                            r"|\breleased by\b|\b(we )?(apply|use|adopt|evaluate|compare)\b[^.]{0,30}\b(their|its)\b", re.I)
# Meerkat's npj version: "The pre-trained weights of Mistral-7B-v0.1 and Meta-Llama-3-8B-Instruct are available at ..."
_BASE_MODEL = re.compile(r"\b(initiali[sz]ed?|initiali[sz]ing|start(ed|ing)? from|fine-?tuned? from|based on|built (up)?on|base models?|"
                         r"backbones?|baselines?|compared? (with|to|against)|teachers?|pre-?trained (weights|models?|checkpoints?) of)\b", re.I)


def score_weights_candidates(text: str, title: str | None = None, authors=None, code_url: str | None = None) -> list[dict]:
    """Hugging Face model repos printed in a paper as its own released weights. Meerkat (P6497 of llm-to-domain-slm)
    prints huggingface.co/dmis-lab/llama-3-meerkat-8b-v1.0 after 'We will publicly release all the artifacts including
    our model weights', but that model card cites other papers' arXiv ids, not its own, so HF's filter=arxiv never finds
    it. Signals: the sentence before the URL says the authors release it (+3); it names a base model, a baseline or a
    teacher and no release (-4); owner matches an author or the code repo's owner (+2); first page (+1). Conversion
    owners (TheBloke, mlx-community, ...) are dropped. Best first; the paper's own count from WEIGHTS_MIN_SCORE."""
    text = text or ""
    atoks = {t for t in _author_tokens(authors) if len(t) >= 5}
    owner_keys = _repo_owner_keys(code_url)
    first_page = min(6000, max(2000, len(text) // 8))
    found: dict[str, dict] = {}
    url_ends = [u.end() for u in re.finditer(r"(?:https?://|(?<![\w/.])huggingface\.co/)\S+", text)]
    # a collection page counts like a model (Meerkat's npj version: "Our models are publicly available at our official
    # Hugging Face repository (https://huggingface.co/collections/dmis-lab/meerkat-...)"); enrich expands it to models
    hits = sorted([(m, False) for m in _HF_MODEL_URL.finditer(text)] + [(m, True) for m in _HF_COLLECTION_URL.finditer(text)],
                  key=lambda x: x[0].start())
    for m, coll in hits:
        owner, repo = m.group(1), m.group(2)
        # the words since the previous URL: MMedLM's '... can be found in <its model> with the Llama 3 license <meta-llama
        # URL>' is about its own model, not the licence link after it
        lo = max([e for e in url_ends if e <= m.start()] or [0])
        repo = re.sub(r"(v\d+\.\d)\d{1,3}$", r"\1", repo)     # a footnote number glued on: 'meerkat-7b-v1.033' -> 'v1.0'
        if not repo or owner.lower() in HF_CONVERSION_OWNERS:
            continue
        sent, _ = _repo_sentence(text[lo:m.start()], m.start() - lo)
        s, why = 0, []
        own = _OWN_WEIGHTS.search(sent) or _OWN_CODE.search(sent)
        theirs = _THEIR_RELEASE.search(sent)
        if theirs:
            s -= 4; why.append(f"someone else's release {theirs.group(0)[:30]!r}")
        elif own:
            s += 3; why.append(f"release phrase {own.group(0)[:40]!r}")
        elif _BASE_MODEL.search(sent):
            s -= 4; why.append("names a base model, baseline or teacher")
        elif _FOUND_AT.search(sent):
            s += 2; why.append(f"{_FOUND_AT.search(sent).group(0)[:30]!r}")
        if s >= 0 and _AVAIL_SECTION.search(text[max(0, m.start() - 1500):m.start()]):
            s += 2; why.append("in a code/data availability section")
        o = norm_name(owner)
        if any(t in o for t in atoks):
            s += 2; why.append("owner matches an author")
        elif o in owner_keys:
            s += 2; why.append("owner matches the code repo's owner")
        if m.start() < first_page:
            s += 1; why.append("first page")
        mid = f"collections/{owner}/{repo}" if coll else f"{owner}/{repo}"
        key = mid.lower()
        if key not in found or s > found[key]["score"]:
            q = re.sub(r"\s+", " ", text[max(0, m.start() - 160):m.end() + 5]).strip()
            found[key] = {"id": mid, "url": f"https://huggingface.co/{mid}", "score": s, "why": why, "collection": coll,
                          "owner": owner, "quote": q, "pos": found[key]["pos"] if key in found else m.start()}
    # a footnote run lists several models after one release sentence (Meerkat: 7B, 8B, 70B): a model of the same owner
    # as one that passes, and not itself named as a base model, passes too
    passing = {norm_name(c["owner"]) for c in found.values() if c["score"] >= WEIGHTS_MIN_SCORE}
    for c in found.values():
        if WEIGHTS_MIN_SCORE > c["score"] >= 0 and norm_name(c["owner"]) in passing:
            c["why"].append(f"same owner as a released model (score {c['score']} -> {WEIGHTS_MIN_SCORE})")
            c["score"] = WEIGHTS_MIN_SCORE
    return sorted(found.values(), key=lambda c: (-c["score"], c["pos"]))


def hf_collection_models(rv: "Review", owner: str, slug: str) -> list[str]:
    """The models in a Hugging Face collection that its owner holds. pdftotext drops the hyphen before the 24-digit id
    ('meerkat6710b7...' for 'meerkat-6710b7...'): that form is tried as well."""
    for sl in dict.fromkeys([slug, re.sub(r"(?<=[A-Za-z0-9])-?([0-9a-f]{24})$", r"-\1", slug)]):
        st, d = http_json(f"https://huggingface.co/api/collections/{owner}/{sl}", rv.cache_dir, ttl=TTL_SEARCH)
        if st == 200 and isinstance(d, dict):
            return [i["id"] for i in d.get("items") or [] if i.get("type") == "model" and isinstance(i.get("id"), str)
                    and i["id"].split("/")[0].lower() == owner.lower()]
    return []


def hf_model_facts(rv: "Review", model_id: str) -> dict:
    """Licence, parameter count and loader of one Hugging Face model (its /api/models record; free, cached): what a team
    needs to know before it evaluates the weights. Empty when the record cannot be read."""
    st, d = http_json(f"https://huggingface.co/api/models/{model_id}", rv.cache_dir, ttl=TTL_SEARCH)
    if st != 200 or not isinstance(d, dict):
        return {}
    lic = (d.get("cardData") or {}).get("license") or next((t.split(":", 1)[1] for t in d.get("tags") or [] if t.startswith("license:")), None)
    params = (d.get("safetensors") or {}).get("total")
    out = {"license": lic if isinstance(lic, str) else None, "params": params if isinstance(params, int) else None,
           "library": d.get("library_name") if isinstance(d.get("library_name"), str) else None}
    return {k: v for k, v in out.items() if v}


def readme_checkpoint_line(md: str | None, title: str | None = None, authors=None, code_url: str | None = None,
                           cited: set | None = None) -> str | None:
    """First README line that links released weights. Unchecked TODOs and 'under construction' sections do not count.
    Under a weights heading, the link whose path names the paper's short name ('MoGe-2' -> .../moge-2-vitl) is chosen
    over the first one, since a repo shared by several versions lists all of them. With a title, Hugging Face links that
    are not the paper's own (a retriever or base model it uses, see _own_weights_link) are skipped, unless the model is
    in `cited` (lower-case ids of HF models whose card cites the paper): MMedLM's README links Henrychur/MMed-Llama-3-8B,
    which cites the paper, but its owner matches no author and its name holds neither a short name nor the repo's."""
    def own(u):
        hm = re.search(r"huggingface\.co/([^/?#\s]+/[^/?#\s`)\]]+)", u)
        return bool(cited and hm and hm.group(1).lower().rstrip(".") in cited) or _own_weights_link(u, title, authors, code_url)
    # image-link badges ([![DOI](zenodo badge)](doi.org/...zenodo...), shields) point at code archives, not weights (P637)
    lines = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", md or "").splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or re.match(r"^[-*]\s*\[ \]", line) or re.search(r"under construction|coming soon|to be released|will be released", line, re.I):
            continue
        if _CKPT_WORDS.search(line) and (_CKPT_HOST.search(line) or re.search(r"checkpoints? (are|is) (published|available)", line, re.I)):
            hosts = [u for u in re.findall(r"https?://[^\s\"'<>)`\]]+", line) if _CKPT_HOST.search(u)]
            if hosts and not any(own(u) for u in hosts):
                continue
            return line[:300]
        if re.match(r"^#+\s*.*(checkpoints?|model zoo|pre-?trained|models)\b", line, re.I):
            # the section up to the next heading (max 80 lines): MoGe lists its weights in a table under the heading
            sec = []
            for l in lines[i + 1:i + 81]:
                if re.match(r"^\s*#+\s", l):
                    break
                sec.append(l.strip())
            nxt = " ".join(x for x in sec if x)
            if re.search(r"under construction|coming soon", nxt, re.I):
                continue
            if _CKPT_HOST.search(nxt):
                us = [u for u in re.findall(r"https?://[^\s\"'<>)`]+", nxt) if _CKPT_HOST.search(u) and own(u)]
                if not us:
                    continue
                short = norm_name((title or "").split(":")[0]) if ":" in (title or "") else ""
                cm = re.search(r"github\.com/[^/]+/([^/#?\s]+)", code_url or "")
                code = norm_name(cm.group(1)) if cm else ""
                hf_repo = lambda u: norm_name(u.rstrip("/").rsplit("/", 1)[-1])
                # Minitron's README lists Mistral-NeMo-Minitron-8B-Base first; the paper's own is Minitron-8B-Base
                u = (next((u for u in us if short and short in norm_name(u)), None)
                     or next((u for u in us if len(code) >= 4 and hf_repo(u).startswith(code)), None) or us[0])
                return (line + " " + u)[:300]
    return None


def _curl_public(url: str, ua: str, hops: int = 5) -> bytes:
    """GET with curl, following redirects by hand: each hop must pass _public_url first. `curl -L` followed a public
    URL's redirect into the researcher's network (security scan F13, follow-up 2026-10-21). b"" on any failure."""
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "body"
        for _ in range(hops + 1):
            try:
                r = subprocess.run(["curl", "-s", "--proto", "=http,https", "--max-time", str(REQUEST_TIMEOUT), "-A", ua,
                                    "-o", str(out), "-w", "%{http_code} %{redirect_url}", "--", url],
                                   capture_output=True, timeout=REQUEST_TIMEOUT + 10)
            except Exception:
                return b""
            code, _, nxt = (r.stdout or b"").decode(errors="replace").strip().partition(" ")
            if not code.startswith("3"):
                return out.read_bytes() if out.exists() else b""
            if not nxt or not _public_url(nxt):
                return b""
            out.unlink(missing_ok=True)
            url = nxt
    return b""


def pdf_text_local(url: str, cache_dir: Path) -> str | None:
    """Download a PDF and convert it with `pdftotext` (poppler) when the HTML and jina routes fail. None if unavailable."""
    import shutil, subprocess, tempfile
    exe = shutil.which("pdftotext")
    if not exe or not _public_url(url):      # an OpenAlex pdf_url: never file://, ftp:// or an internal host (F13)
        return None
    req = urllib.request.Request(url, headers={"User-Agent": f"litrev/1.0 (+{MAILTO or 'mailto:unset'})"})
    req.public_only = True
    try:
        with _urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            data = r.read()
    except Exception:
        return None
    if not data.startswith(b"%PDF") and shutil.which("curl"):
        # some publishers (nature.com) answer Python's TLS client with a JavaScript challenge page but serve curl the PDF
        data = _curl_public(url, req.get_header("User-agent"))
    if not data.startswith(b"%PDF"):
        return None
    with tempfile.TemporaryDirectory() as td:
        pdf, txt = Path(td) / "p.pdf", Path(td) / "p.txt"
        pdf.write_bytes(data)
        try:
            subprocess.run([exe, str(pdf), str(txt)], check=True, capture_output=True, timeout=120)
        except Exception:
            return None
        return txt.read_text(errors="replace") if txt.exists() else None


def check_details(rv: "Review", pid: str) -> list[dict]:
    """Quotes in data/details/<pid>.json that merge-details would reject (same matching rule). Lets extractors self-check."""
    f = rv.path / "data" / "details" / f"{pid}.json"
    det = json.loads(f.read_text())
    text, _ = rv.read_fulltext(pid)
    text = text or ""
    bad = []
    for field in DETAIL_FIELDS:
        e = det.get(field)
        if isinstance(e, dict) and e.get("value") is not None and e.get("status", "author-reported") == "author-reported" and not quote_in_text(e.get("quote"), text):
            bad.append({"field": field, "quote": e.get("quote")})
    for i, du in enumerate(det.get("datasets_used") or []):
        if du and du.get("quote") and not quote_in_text(du["quote"], text):
            bad.append({"field": f"datasets_used[{i}]", "quote": du["quote"]})
    for i, r in enumerate(det.get("results") or []):
        if isinstance(r, dict) and not quote_in_text(r.get("quote"), text):
            bad.append({"field": f"results[{i}]", "quote": r.get("quote")})
    return bad


_SPLIT_FREE_TEXT = re.compile(r"[\d(),;:]")


def details_row_notes(det: dict) -> list[str]:
    """Result rows a step-5 file should fix before merge-details: a `row_kind` that is missing or not one of ROW_KINDS,
    and a `split` that carries free text (sizes, sampling, subsets) instead of the split's name. table-qa 2026-10-29:
    WTQ's test set came in 15+ labels ("test (4,344)", "official test, 4,344 questions") and 1,422 rows had to be
    normalised before a KPI could be added; the name goes in `split`, the rest in `split_note`."""
    notes = []
    for i, r in enumerate(det.get("results") or []):
        if not isinstance(r, dict):
            continue
        if r.get("row_kind") not in ROW_KINDS:
            notes.append(f"results[{i}]: row_kind {r.get('row_kind')!r} - set one of {', '.join(ROW_KINDS)}")
        sp = str(r.get("split") or "")
        if sp and (_SPLIT_FREE_TEXT.search(sp) or len(sp.split()) > 3):
            notes.append(f"results[{i}]: split {sp!r} - keep only the split's name (test, dev, small-test, subset, model-level, "
                         f"not stated) and move sizes or sampling to split_note")
    return notes


DEFAULT_MAX_PAPERS = 128   # included non-seminal papers (40 until v1.14.4, 100 until v1.16.0)
SKILL_VERSION = "1.37.0"    # the plugin version; bump with plugin.json and marketplace.json on every release
REVIEW_DIRS = ("data", "data/details", "data/seed_taxonomy", "data/datasets_step3", "data/screening", "data/clusters",
               "data/explainers", "data/adjacent", "data/presentation", "data/presentation/script", "cache/http",
               "cache/fulltext", "rendered")


def needs_details(p: dict) -> bool:
    """Included papers that get a step-5 extractor: everything but surveys (taxonomy sources included)."""
    return p.get("status") == "included" and not p.get("is_survey") and not p.get("taxonomy_source")


def details_status(rv: "Review", quotes: bool = True) -> dict:
    """Step-5 coverage: included non-survey papers with no data/details/<pid>.json (an agent died or never ran),
    files that are not valid JSON, and per paper the quotes merge-details would reject. Read-only."""
    ddir = rv.path / "data" / "details"
    out = {"missing": [], "invalid": [], "ok": [], "rejected_quotes": {}, "not_included": []}
    for p in sorted(rv.tables["papers"].values(), key=lambda p: p["id"]):
        if not needs_details(p):
            continue
        f = ddir / f"{p['id']}.json"
        if not f.exists():
            out["missing"].append(p["id"])
            continue
        try:
            bad = check_details(rv, p["id"]) if quotes else (json.loads(f.read_text()) and [])   # quote checks: ~0.5 s/paper
        except (json.JSONDecodeError, UnicodeDecodeError):
            out["invalid"].append(p["id"])
            continue
        out["ok"].append(p["id"])
        if bad:
            out["rejected_quotes"][p["id"]] = len(bad)
    for f in sorted(ddir.glob("*.json")) if ddir.exists() else []:
        p = rv.tables["papers"].get(f.stem)
        if p is None or not needs_details(p):
            out["not_included"].append(f.stem)
    return out


# --------------------------------------------------------------------------
# Review store
# --------------------------------------------------------------------------

def new_blank(table: str, id_: str) -> dict:
    """Blank record with every column of the table present (None/[]), so imports and renders share one shape."""
    shapes = {
        "search_terms": {"term": None, "aliases": [], "category": "concept", "origin": None, "rationale": None, "related_ids": [],
                         "run_ids": [], "discovered_paper_ids": [], "status": "pending", "status_reason": None},
        "search_runs": {"kind": "keyword", "query": None, "term_ids": [], "source": None, "query_url": None, "executed_on": today(),
                        "filters": {}, "seed_paper_id": None, "direction": None, "scope": None, "retrieved": None, "screened": None, "limitations": None},
        "papers": {"ids": {"arxiv": None, "doi": None, "openalex": None, "s2": None}, "title": None, "authors": [], "labs": None, "year": None,
                   "venue": None, "published_venue": None, "type": None, "version": None, "url": None, "abstract": None, "categories": [], "citations": None,
                   "relevance": None, "contribution": None, "status": "candidate", "status_reason": None, "is_survey": False, "seminal": False,
                   "paper_role": "method", "paper_role_reason": None, "task_ids": [], "dataset_ids": [], "benchmark_ids": [], "metric_ids": [], "result_ids": [], "code": None, "checkpoint": None,
                   "data": None, "representation": None, "loss": None, "topology": None, "training_scheme": None, "evidence_limitations": None,
                   "figures": None,
                   "discovery_run_ids": [], "sources": []},
        "tasks": {"name": None, "aliases": [], "definition": None, "inputs": None, "outputs": None, "relevance": None, "parent_id": None,
                  "related_ids": [], "paper_ids": [], "dataset_ids": [], "benchmark_ids": [], "metric_ids": [], "sources": []},
        "datasets": {"name": None, "version": None, "aliases": [], "status": "included", "task_ids": [], "ground_truth": None, "size": None,
                     "availability": None, "real_synthetic": None, "leaderboard": None, "modality": None, "paper_ids": [], "benchmark_ids": [],
                     "metric_ids": [], "sources": [], "limitations": None},
        "benchmarks": {"name": None, "version": None, "aliases": [], "task_ids": [], "dataset_ids": [], "split": None, "protocol": None,
                       "challenge": None, "constraints": None, "metric_ids": [], "eval_server": None, "leaderboard": None, "docs_url": None,
                       "paper_ids": [], "sources": []},
        "metrics": {"name": None, "aliases": [], "status": "included", "definition": None, "units_range": None, "higher_is_better": None,
                    "aggregation": None, "thresholds": None, "task_ids": [], "benchmark_ids": [], "implementation_url": None, "limitations": None,
                    "kind": "published", "paper_ids": [], "sources": []},
        "results": {"paper_id": None, "method_variant": None, "checkpoint": None, "benchmark_id": None, "dataset_id": None, "task_id": None,
                    "dataset_version": None, "split": None, "split_note": None, "track_year": None, "protocol": None, "metric_id": None,
                    "metric_variant": None, "value": None, "units": None, "rank": None, "rank_denominator": None, "snapshot_date": None,
                    "provenance": "paper-reported", "row_kind": None, "training_data": None, "test_time_adaptations": None, "compute": None,
                    "source": None, "location": None, "quote": None, "caveats": None, "status": "author-reported"},
        "clusters": {"name": None, "definition": None, "distinguishing_trait": None, "survey_family": None, "members": [],
                     "paper_ids": [], "winner_paper_id": None, "winner_reason": None},
        "explainers": {"cluster_id": None, "paper_id": None, "headline": None, "problem": None, "key_idea": None,
                       "how_it_works": None, "vs_siblings": None, "figure": None, "glossary": [], "facets": {},
                       "details": {}, "diagram": None, "declared_limitations": [], "weak_points": [],
                       "member_limitations": {}},
    }
    rec = {"id": id_}
    rec.update(json.loads(json.dumps(shapes[table])))
    return rec


class Review:
    def __init__(self, path: Path, meta: dict, tables: dict):
        # absolute: the voice worker and Chrome run in other folders (`video --review .` failed in both)
        self.path = Path(path).resolve()
        self.meta = meta
        self.tables = tables

    # ----- lifecycle -----
    @classmethod
    def init(cls, path, slug: str, purpose: str, seed: str, max_papers: int = None, years: int = 3,
             seminal_citations: int = 1000, categories: list[str] | None = None, max_seminal: int = 10) -> "Review":
        path = Path(path)
        if (path / "meta.json").exists():
            return cls.load(path)
        meta = {
            "slug": slug, "purpose": purpose, "seed": seed,
            "caps": {"max_papers": DEFAULT_MAX_PAPERS if max_papers is None else max_papers, "years": years, "seminal_citations": seminal_citations, "max_seminal": max_seminal},
            "categories": categories or [], "hub_url": None, "created_at": now_iso(), "updated_at": now_iso(),
            "counters": {p: 0 for p in PREFIX.values()},
            "step_log": {"init": now_iso()},      # `status` treats a review born with the log as fully tracked
        }
        tables = {t: ([] if t in LIST_TABLES else {}) for t in TABLES}
        rv = cls(path, meta, tables)
        for d in REVIEW_DIRS:
            (path / d).mkdir(parents=True, exist_ok=True)
        rv.save()
        return rv

    @classmethod
    def load(cls, path) -> "Review":
        path = Path(path)
        meta = json.loads((path / "meta.json").read_text())
        for d in REVIEW_DIRS:      # every `> data/<dir>/...` redirect target exists, also on older reviews
            (path / d).mkdir(parents=True, exist_ok=True)
        tables = {}
        for t in TABLES:
            f = path / "data" / f"{t}.json"
            if f.exists():
                tables[t] = json.loads(f.read_text())
            else:
                tables[t] = [] if t in LIST_TABLES else {}
        for s in tables["search_runs"].values():   # runs saved before v1.23.1 kept the credentials _oa() adds
            if s.get("query_url"):
                s["query_url"] = _drop_credentials(s["query_url"])
        return cls(path, meta, tables)

    def save(self):
        """Write meta + every table. Each file is replaced atomically, so a concurrent reader never sees half a file."""
        self.meta["updated_at"] = now_iso()
        (self.path / "data").mkdir(parents=True, exist_ok=True)

        def put(f: Path, obj):
            tmp = f.with_name(f".{f.name}.tmp")
            tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
            os.replace(tmp, f)
        put(self.path / "meta.json", self.meta)
        for t in TABLES:
            put(self.path / "data" / f"{t}.json", self.tables[t])

    @property
    def cache_dir(self) -> Path:
        return self.path / "cache"

    def new_id(self, prefix: str) -> str:
        self.meta["counters"][prefix] = self.meta["counters"].get(prefix, 0) + 1
        return f"{prefix}{self.meta['counters'][prefix]:03d}"

    # ----- fulltext -----
    def fulltext_path(self, pid: str) -> Path:
        return self.path / "cache" / "fulltext" / f"{safe_id(pid)}.txt"

    def fulltext_meta_path(self, pid: str) -> Path:
        return self.path / "cache" / "fulltext" / f"{safe_id(pid)}.meta.json"

    def write_fulltext_meta(self, pid: str, url: str, method: str):
        self.fulltext_meta_path(pid).parent.mkdir(parents=True, exist_ok=True)
        self.fulltext_meta_path(pid).write_text(json.dumps({"url": url, "method": method, "fetched_at": now_iso()}))

    def read_fulltext(self, pid: str) -> tuple[str | None, dict]:
        p = self.fulltext_path(pid)
        meta = {}
        if self.fulltext_meta_path(pid).exists():
            meta = json.loads(self.fulltext_meta_path(pid).read_text())
        return (p.read_text(errors="replace") if p.exists() else None), meta

    # ----- entity helpers -----
    def find_paper(self, ids: dict | None, title: str | None = None) -> dict | None:
        ids = ids or {}
        ax, doi, oa = norm_arxiv(ids.get("arxiv")), norm_doi(ids.get("doi")), norm_openalex(ids.get("openalex"))
        s2 = ids.get("s2")
        nt = norm_title(title)
        for p in self.tables["papers"].values():
            pi = p.get("ids") or {}
            if ax and pi.get("arxiv") == ax:
                return p
            if doi and pi.get("doi") == doi:
                return p
            if oa and pi.get("openalex") == oa:
                return p
            if s2 and pi.get("s2") == s2:
                return p
        if nt and len(nt) > 20:
            for p in self.tables["papers"].values():
                if norm_title(p.get("title")) == nt:
                    return p
        return None

    def upsert_paper(self, rec: dict, source_run_id: str | None = None) -> dict:
        """Insert or merge a paper record. Never renumbers; fills blanks, merges ids."""
        ids_in = rec.get("ids") or {}
        norm_ids = {"arxiv": norm_arxiv(ids_in.get("arxiv")), "doi": norm_doi(ids_in.get("doi")),
                    "openalex": norm_openalex(ids_in.get("openalex")), "s2": ids_in.get("s2")}
        p = self.find_paper(norm_ids, rec.get("title"))
        if p is None:
            p = {
                "id": self.new_id("P"), "ids": {"arxiv": None, "doi": None, "openalex": None, "s2": None},
                "title": None, "authors": [], "labs": None, "year": None, "venue": None, "published_venue": None, "type": None, "version": None,
                "url": None, "abstract": None, "categories": [], "citations": evidence(None, "not-found"),
                "relevance": None, "contribution": None, "status": "candidate", "status_reason": None,
                "is_survey": False, "seminal": False, "paper_role": "method", "paper_role_reason": None,
                "task_ids": [], "dataset_ids": [], "benchmark_ids": [], "metric_ids": [], "result_ids": [],
                "code": None, "checkpoint": None,
                "data": None, "representation": None, "loss": None, "topology": None, "training_scheme": None,
                "evidence_limitations": None, "discovery_run_ids": [], "sources": [],
            }
            self.tables["papers"][p["id"]] = p
        for k, v in norm_ids.items():
            if v and not p["ids"].get(k):
                p["ids"][k] = v
        for k, v in rec.items():
            if k == "ids":
                continue
            if k in ("authors", "categories", "sources") and v:
                for x in v:
                    if x not in p.setdefault(k, []):
                        p[k].append(x)
            elif k == "published_venue" and is_evidence(v):
                rk = lambda e: {"verified": 3, "author-reported": 2}.get(e.get("status"), 1) if is_evidence(e) else 0
                if rk(v) > rk(p.get("published_venue")):
                    p["published_venue"] = v
            elif k == "citations" and is_evidence(v):
                cur = p.get("citations")
                if not is_evidence(cur) or cur.get("value") is None or (v.get("value") or 0) > (cur.get("value") or 0):
                    p["citations"] = v
            elif p.get(k) in (None, [], "") and v not in (None, [], ""):
                p[k] = v
        if source_run_id and source_run_id not in p["discovery_run_ids"]:
            p["discovery_run_ids"].append(source_run_id)
        if p.get("title") and re.search(r"\b(survey|review|overview|tutorial)\b", p["title"], re.I):
            p["is_survey"] = True
        return p

    def add_term(self, term: str, category: str, origin: str, rationale: str, aliases: list[str] | None = None,
                 related_ids: list[str] | None = None) -> dict:
        for q in self.tables["search_terms"].values():
            if norm_name(q["term"]) == norm_name(term):
                return q
        q = {"id": self.new_id("Q"), "term": term, "aliases": aliases or [], "category": category,
             "origin": origin, "rationale": rationale, "related_ids": related_ids or [],
             "run_ids": [], "discovered_paper_ids": [], "status": "pending", "status_reason": None}
        self.tables["search_terms"][q["id"]] = q
        return q

    def add_run(self, kind: str, query: str, term_ids: list[str], source: str, query_url: str | None,
                scope: str | None = None, retrieved: int | None = None, screened: int | None = None,
                seed_paper_id: str | None = None, direction: str | None = None, filters: dict | None = None,
                limitations: str | None = None) -> dict:
        s = {"id": self.new_id("S"), "kind": kind, "query": query, "term_ids": list(term_ids or []), "source": source,
             "query_url": _drop_credentials(query_url), "executed_on": today(), "filters": filters or {}, "seed_paper_id": seed_paper_id,
             "direction": direction, "scope": scope, "retrieved": retrieved, "screened": screened, "limitations": limitations}
        self.tables["search_runs"][s["id"]] = s
        return s

    def add_link(self, run_id: str, paper_id: str, discovery_type: str, origin: str | None, decision: str, reason: str | None) -> dict:
        for l in self.tables["discovery_links"]:
            if l["run_id"] == run_id and l["paper_id"] == paper_id and l["discovery_type"] == discovery_type and l.get("origin") == origin:
                return l
        l = {"run_id": run_id, "paper_id": paper_id, "discovery_type": discovery_type, "origin": origin,
             "decision": decision, "reason": reason}
        self.tables["discovery_links"].append(l)
        return l

    def _find_by_name(self, table: str, name: str, exact: bool = False) -> dict | None:
        """Match when the name equals a record's name or alias after normalisation (case, punctuation, whitespace).
        'KITTI (Eigen split)' also tries 'KITTI Eigen split', 'Eigen split KITTI' and the parenthetical-stripped 'KITTI'.
        The parenthetical alone is never tried: 'TREC DL 2019+2020 (MS MARCO passage)' names TREC DL, not its corpus.
        exact=True skips the fallback (for creating records: 'MegaDepth-1500 (LO-RANSAC)' is not 'MegaDepth-1500')."""
        if not name:
            return None
        cands = [name]
        m = None if exact else re.match(r"^\s*(.*?)\s*\((.*?)\)\s*$", name)
        if m:
            outer, inner = m.group(1), m.group(2)
            cands += [f"{outer} {inner}", f"{inner} {outer}", outer]
        for c in cands:
            k = norm_name(c)
            if not k:
                continue
            for e in self.tables[table].values():
                if norm_name(e.get("name")) == k or any(norm_name(a) == k for a in e.get("aliases") or []):
                    return e
        return None

    def add_task(self, name: str, definition=None, inputs=None, outputs=None, relevance=None, aliases=None,
                 parent_id=None, related_ids=None, sources=None, exact: bool = False) -> dict:
        t = self._find_by_name("tasks", name, exact)
        if t:
            return t
        t = {"id": self.new_id("T"), "name": name, "aliases": aliases or [], "definition": definition or evidence(None, "not-found"),
             "inputs": inputs, "outputs": outputs, "relevance": relevance or evidence(None, "not-found"),
             "parent_id": parent_id, "related_ids": related_ids or [], "paper_ids": [], "dataset_ids": [],
             "benchmark_ids": [], "metric_ids": [], "sources": sources or []}
        self.tables["tasks"][t["id"]] = t
        return t

    def add_dataset(self, name: str, aliases=None, version=None, status: str = "included", exact: bool = False, **fields) -> dict:
        d = self._find_by_name("datasets", name, exact)
        if d:
            return d
        d = {"id": self.new_id("D"), "name": name, "version": version, "aliases": aliases or [], "status": status,
             "task_ids": [], "ground_truth": None, "size": None, "availability": None, "real_synthetic": None,
             "leaderboard": None, "modality": None, "paper_ids": [], "benchmark_ids": [], "metric_ids": [],
             "sources": [], "limitations": None}
        d.update(fields)
        self.tables["datasets"][d["id"]] = d
        return d

    def add_benchmark(self, name: str, aliases=None, version=None, exact: bool = False, **fields) -> dict:
        b = self._find_by_name("benchmarks", name, exact)
        if b:
            return b
        b = {"id": self.new_id("B"), "name": name, "version": version, "aliases": aliases or [], "task_ids": [], "dataset_ids": [],
             "split": None, "protocol": None, "challenge": None, "constraints": None, "metric_ids": [], "eval_server": None,
             "leaderboard": None, "docs_url": None, "paper_ids": [], "sources": []}
        b.update(fields)
        self.tables["benchmarks"][b["id"]] = b
        return b

    def add_metric(self, name: str, aliases=None, higher_is_better=None, status: str = "included", exact: bool = False, **fields) -> dict:
        m = self._find_by_name("metrics", name, exact)
        if m:
            return m
        m = {"id": self.new_id("M"), "name": name, "aliases": aliases or [], "status": status,
             "definition": evidence(None, "not-found"), "units_range": None, "higher_is_better": higher_is_better,
             "aggregation": None, "thresholds": None, "task_ids": [], "benchmark_ids": [], "implementation_url": None,
             "limitations": None, "kind": "published", "paper_ids": [], "sources": []}
        m.update(fields)
        self.tables["metrics"][m["id"]] = m
        return m

    def add_result(self, **r) -> dict:
        rec = new_blank("results", self.new_id("R"))
        rec.update(r)
        self.tables["results"][rec["id"]] = rec
        return rec

    def _unused_add_result(self, **r) -> dict:
        rec = {"id": self.new_id("R"), "paper_id": None, "method_variant": None, "checkpoint": None, "benchmark_id": None,
               "dataset_id": None, "task_id": None, "dataset_version": None, "split": None, "track_year": None, "protocol": None,
               "metric_id": None, "metric_variant": None, "value": None, "units": None, "rank": None, "rank_denominator": None,
               "snapshot_date": None, "provenance": "paper-reported", "training_data": None, "test_time_adaptations": None,
               "compute": None, "source": None, "location": None, "quote": None, "caveats": None, "status": "author-reported"}
        rec.update(r)
        self.tables["results"][rec["id"]] = rec
        return rec

    # ----- derived fields -----
    def derive(self):
        runs, links, papers = self.tables["search_runs"], self.tables["discovery_links"], self.tables["papers"]
        results = self.tables["results"]
        # One-pass indexes: the nested scans they replace cost 67 s per write command at 7k papers x 46k results.
        runs_by_term, papers_by_run, runs_by_paper = defaultdict(list), defaultdict(set), defaultdict(set)
        for s in runs.values():
            for t in dict.fromkeys(s.get("term_ids") or []):
                runs_by_term[t].append(s["id"])
        for l in links:
            papers_by_run[l["run_id"]].add(l["paper_id"])
            runs_by_paper[l["paper_id"]].add(l["run_id"])
        results_by_paper, metrics_by_dataset = defaultdict(list), defaultdict(set)
        for r in results.values():
            results_by_paper[r.get("paper_id")].append(r["id"])
            if r.get("dataset_id") and r.get("metric_id"):
                metrics_by_dataset[r["dataset_id"]].add(r["metric_id"])
        for q in self.tables["search_terms"].values():
            q["run_ids"] = list(runs_by_term.get(q["id"], []))
            q["discovered_paper_ids"] = sorted({pid for rid in set(q["run_ids"]) for pid in papers_by_run.get(rid, ()) if pid in papers})
            if q["status"] == "pending" and q["run_ids"]:
                q["status"] = "searched"
        for p in papers.values():
            if p.get("is_survey") and not p.get("type"):
                p["type"] = "survey"
            elif p.get("is_survey") and p.get("type") == "preprint":
                p["type"] = "survey"
            p["discovery_run_ids"] = sorted(runs_by_paper.get(p["id"], set()) | set(p.get("discovery_run_ids") or []))
            p["result_ids"] = sorted(results_by_paper.get(p["id"], []))
        for tbl, key in (("tasks", "task_ids"), ("datasets", "dataset_ids"), ("benchmarks", "benchmark_ids"), ("metrics", "metric_ids")):
            papers_by_entity = defaultdict(set)
            for p in papers.values():
                for x in p.get(key) or []:
                    papers_by_entity[x].add(p["id"])
            for e in self.tables[tbl].values():
                e["paper_ids"] = sorted(papers_by_entity.get(e["id"], ()))
        for r in results.values():
            for tbl, key in (("datasets", "dataset_id"), ("benchmarks", "benchmark_id"), ("metrics", "metric_id")):
                e = self.tables[tbl].get(r.get(key) or "")
                if e is not None and r["paper_id"] in papers and r["paper_id"] not in e["paper_ids"]:
                    e["paper_ids"].append(r["paper_id"])
        for d in self.tables["datasets"].values():
            d["metric_ids"] = sorted(metrics_by_dataset.get(d["id"], set()) | set(d.get("metric_ids") or []))
        for l in links:
            if l["paper_id"] in papers and papers[l["paper_id"]]["status"] in ("included", "excluded"):
                l["decision"] = papers[l["paper_id"]]["status"]
                if papers[l["paper_id"]]["status"] == "excluded" and not l.get("reason"):
                    l["reason"] = papers[l["paper_id"]].get("status_reason")
        self._derive_clusters()

    def _derive_clusters(self):
        C = self.tables.get("clusters") or {}
        if not C:
            return
        rp = rank_papers(self) if self.meta.get("kpis") else {"ranked": [], "below_cut": [], "top_n": 0}
        pos = {r["paper_id"]: r for r in rp["ranked"] + rp["below_cut"]}
        for c in C.values():
            ids: list[str] = []
            for m in c.get("members") or []:
                if m.get("paper_id") and m["paper_id"] not in ids:
                    ids.append(m["paper_id"])
            c["paper_ids"] = ids
            ranked = [pos[p] for p in ids if p in pos]
            if ranked:
                # the cluster's example is its best-ranked peer-reviewed member; a preprint only when none is published
                pub = [r for r in ranked if r.get("venue")]
                w = min(pub or ranked, key=lambda r: r["position"])
                top = min(ranked, key=lambda r: r["position"])
                c["winner_paper_id"] = w["paper_id"]
                c["winner_reason"] = ((f"best-ranked published member ({w['venue']}): " if pub else "best-ranked member (no published venue found for any member): ")
                                      + f"KPI position {w['position']}: rank {w['best_rank']}/{w['denominator']} on "
                                      f"{w['best_kpi']} (variant {w.get('method_variant') or 'not stated'})"
                                      + (f"; below the top-{rp['top_n']} cut" if w["position"] > rp["top_n"] else "")
                                      + (f"; {top['paper_id']} (no published venue found) ranks higher at position {top['position']}" if top is not w else ""))
            else:
                c["winner_paper_id"], c["winner_reason"] = None, "no member is ranked on the KPIs"


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def _score_paper(p: dict, n_links: int, survey_cited: bool, now_year: int) -> tuple[float, bool]:
    import math
    cites = p.get("citations") if is_evidence(p.get("citations")) else None
    cval = cites.get("value") if cites else None
    year = p.get("year") or now_year
    age = max(1, now_year - year + 1)
    s = 0.0
    verified_cites = cval is not None
    if verified_cites:
        s += 2.0 * math.log1p(float(cval) / age)
    s += 1.0 * n_links
    s += 2.0 if survey_cited else 0.0
    s += max(0.0, 1.0 - 0.15 * (age - 1))
    s += PUBLISHED_VENUE_BONUS if is_published(p) else 0.0
    return s, verified_cites


def _is_seed(rv: Review, p: dict) -> bool:
    """A paper the user named as the seed: `seed: true` set by hand, or its arXiv id / exact title appears in meta.seed."""
    if p.get("seed"):
        return True
    s = str(rv.meta.get("seed") or "")
    ax = (p.get("ids") or {}).get("arxiv")
    if ax and re.search(r"(?<![\d.])" + re.escape(ax) + r"(v\d+)?(?![\d])", s):
        return True
    t = norm_title(p.get("title"))
    return len(t) > 20 and t == norm_title(s)


def select_papers(rv: Review, now_year: int | None = None, shares: dict | None = None) -> dict:
    """`shares` ({scope in-item ordinal: min slots}, default meta.caps.shares) reserves slots for papers screened
    on-topic under that item, so a less-cited family is not crowded out by citation rank."""
    now_year = now_year or _dt.date.today().year
    caps = rv.meta["caps"]
    min_year = now_year - int(caps.get("years", 3))
    seminal_c = int(caps.get("seminal_citations", 1000))
    links = rv.tables["discovery_links"]
    n_links = {}
    survey_cited = set()
    # coverage adds one link per included paper a neighbour connects to, and only to papers outside the review: counted
    # in full, 10-22 links let neighbours outrank the core models search found (Depth Anything, 2026-10-11). It is one
    # discovery path, like any other run.
    cov_runs = {rid for rid, r in rv.tables["search_runs"].items() if r.get("kind") == "coverage"}
    cov_counted = set()
    for l in links:
        if l.get("discovery_type") == "survey":
            survey_cited.add(l["paper_id"])
        if l["run_id"] in cov_runs:
            if l["paper_id"] in cov_counted:
                continue
            cov_counted.add(l["paper_id"])
        # keyword hits are direct relevance evidence; citation-walk hits inherit relevance from the seed only
        n_links[l["paper_id"]] = n_links.get(l["paper_id"], 0) + (2.0 if l.get("discovery_type") == "keyword" else 1.0)
    scored, seminal_pool, kept, kept_ids = [], [], 0, set()
    ddir = rv.path / "data" / "details"
    screens = any(q.get("screening") for q in rv.tables["papers"].values())
    for p in rv.tables["papers"].values():
        if p.get("manual"):
            continue  # screened by hand via `screen`; select never overrides a recorded human decision
        if p.get("status") == "included" and (ddir / f"{p['id']}.json").exists():
            # step-5 work exists: a shifted cap must not silently drop it (screen it out to drop it)
            p["status_reason"] = "kept: step-5 details exist (data/details); use `screen` to exclude"
            kept += 1
            kept_ids.add(p["id"])
            continue
        if p.get("taxonomy_source"):
            p["is_survey"] = True
            p["status"], p["status_reason"] = "included", "survey used as taxonomy source; exempt from cap"
            continue
        cval = p["citations"].get("value") if is_evidence(p.get("citations")) else None
        p["seminal"] = bool(cval is not None and cval >= seminal_c)
        if _is_seed(rv, p):
            p["seed"] = True
            p["status"], p["status_reason"] = "included", "seed paper named by the user; exempt from year window and caps"
            continue
        if p.get("year") and p["year"] < min_year and not p["seminal"]:
            p["status"], p["status_reason"] = "excluded", f"outside year window (<{min_year}) and below seminal threshold ({seminal_c} citations)"
            continue
        scr = p.get("screening") or {}
        if scr.get("verdict") == "off-topic":
            # an off-topic verdict on select's own pick (merge-screening records it on included papers without
            # excluding them) must not hold a slot; manual and step-5 papers were skipped above
            p["status"], p["status_reason"] = "excluded", f"screening: off-topic ({scr.get('scope_ref')}): {scr.get('reason')}"
            continue
        if scr.get("verdict") == "needs-text" and _fulltext_method(rv, p["id"]) == "abstract-only":
            # same rule merge-screening applies to candidates: text was needed, fulltext found none -> text access
            p["status"], p["status_reason"] = "excluded", (f"screening: needs the full text to decide ({scr.get('reason')}); "
                                                           "fulltext found no readable copy")
            continue
        if p["seminal"] and p.get("year") and p["year"] < min_year:
            # the exemption lets older classics past the year window; an in-window paper above the threshold competes
            # in the cap, where its citations rank it high (in the pool, SparseGPT 2023 was cut as seminal 12/19)
            if screens and scr.get("verdict") != "on-topic":
                # llm-to-domain-slm: needs-text classics (Intrinsic Dimensionality, Pruning CNNs, Manifold Mixup) came in
                # through the exemption although nobody had screened them in
                p["status"], p["status_reason"] = "excluded", (f"seminal ({cval} citations) but the exemption needs an on-topic "
                                                               f"verdict (verdict: {scr.get('verdict') or 'not screened'}); screen it first")
                continue
            seminal_pool.append(p)
            continue
        sc, vc = _score_paper(p, n_links.get(p["id"], 0), p["id"] in survey_cited, now_year)
        p["_score"] = round(sc, 3)
        p["_score_basis"] = ("citations/yr + discovery paths + survey-cited + recency" if vc else "discovery paths + survey-cited + recency (citations unverified)") \
            + (f" + published venue ({published_venue_label(p)})" if is_published(p) else "")
        scored.append(p)
    # Seminal papers are exempt from the year window but not unbounded: a citation walk from a famous
    # seed returns dozens of >1000-citation papers. Keep the most-cited `max_seminal` (default 10).
    max_seminal = int(caps.get("max_seminal", 10))
    seminal_pool.sort(key=lambda p: -(p["citations"].get("value") or 0))
    for i, p in enumerate(seminal_pool):
        cval = p["citations"].get("value")
        if i < max_seminal:
            p["status"], p["status_reason"] = "included", f"seminal: {cval} citations (>= {seminal_c}); seminal rank {i + 1}/{len(seminal_pool)}"
        else:
            p["status"], p["status_reason"] = "excluded", f"over seminal cap ({max_seminal}): {cval} citations, seminal rank {i + 1}/{len(seminal_pool)}"
    # once the review screens, papers screened on-topic take the slots first; unscreened ones (never screened, or
    # cap-excluded before screening existed) only fill what is left, and are reported (dashcam: 8 such papers entered)
    screening = any(q.get("screening") for q in rv.tables["papers"].values())
    on_topic = lambda p: (p.get("screening") or {}).get("verdict") == "on-topic"
    scored.sort(key=lambda p: (screening and not on_topic(p), -p["_score"], -(p.get("year") or 0), p["id"]))
    manual_in = sum(1 for p in rv.tables["papers"].values() if p.get("manual") and p["status"] == "included" and not p.get("seminal"))
    cap = max(0, int(caps.get("max_papers", DEFAULT_MAX_PAPERS)) - manual_in - kept)  # never negative: over-screening just leaves no room for scored papers
    # share floors: each named scope item first gets its best-ranked on-topic papers up to its floor (already-included
    # manual or kept papers of the item count toward it); the rest of the cap keeps the global order
    shares = (caps.get("shares") or {}) if shares is None else shares
    items = (rv.meta.get("scope") or {}).get("in") or []
    item_of = lambda p: items.index(p["screening"]["scope_ref"]) + 1 \
        if on_topic(p) and p["screening"].get("scope_ref") in items else None
    picked, floor_pos, share_rep = [], {}, {}
    for k, floor in sorted(((int(k), int(v)) for k, v in shares.items() if int(v) > 0)):
        held = sum(1 for q in rv.tables["papers"].values() if item_of(q) == k and q["status"] == "included"
                   and (q["id"] in kept_ids or (q.get("manual") and not q.get("seminal"))))
        pool = [p for p in scored if item_of(p) == k]
        take = [p for p in pool if p["id"] not in floor_pos][:max(0, floor - held)][:max(0, cap - len(picked))]
        for j, p in enumerate(take):
            floor_pos[p["id"]] = f"floor {held + j + 1}/{floor} for scope item {k}; "
        picked += take
        share_rep[str(k)] = {"floor": floor, "filled": held + len(take), "pool": held + len(pool)}
    chosen = set(floor_pos) | {p["id"] for p in [q for q in scored if q["id"] not in floor_pos][:cap - len(picked)]}
    for i, p in enumerate(scored):
        tier = ("screened on-topic, " if on_topic(p) else "not screened on-topic, after the screened ones, ") if screening else ""
        tier = floor_pos.get(p["id"], "") + tier
        if p["id"] in chosen:
            p["status"], p["status_reason"] = "included", f"rank {i + 1}/{len(scored)} ({tier}by {p['_score_basis']}) (score {p['_score']})" \
                if tier else f"rank {i + 1}/{len(scored)} by {p['_score_basis']} (score {p['_score']})"
        else:
            p["status"], p["status_reason"] = "excluded", f"over cap ({cap}): rank {i + 1}/{len(scored)} (score {p['_score']})"
    for p in rv.tables["papers"].values():
        p.pop("_score", None)
        p.pop("_score_basis", None)
    rv.derive()
    inc = sum(1 for p in rv.tables["papers"].values() if p["status"] == "included")
    out = {"included": inc, "excluded": sum(1 for p in rv.tables["papers"].values() if p["status"] == "excluded"), "cap": cap}
    if share_rep:
        out["shares"] = share_rep
    if screening:
        un = [p["id"] for p in scored if p["id"] in chosen and not on_topic(p)]
        if un:
            out["unscreened_included"] = un
            out["next_step"] = (f"{len(un)} papers took cap slots without an on-topic verdict: `screen-input --status included` "
                                "-> subagent -> `merge-screening`, then `select` again")
    return out


def merge_shares(rv: Review, specs: list[str]) -> dict:
    """meta.caps.shares updated by `K=N` specs (N=0 removes K); K must name a scope in item."""
    out = dict(rv.meta["caps"].get("shares") or {})
    n_in = len((rv.meta.get("scope") or {}).get("in") or [])
    for spec in specs:
        m = re.fullmatch(r"\s*(\d+)\s*=\s*(\d+)\s*", spec)
        if not m or not 1 <= int(m.group(1)) <= n_in:
            raise SystemExit(f"--share {spec!r}: expected K=N with K a scope in item 1..{n_in}")
        out.pop(m.group(1), None) if int(m.group(2)) == 0 else out.__setitem__(m.group(1), int(m.group(2)))
    return out


def select_preview(rv: Review, n: int = 30, now_year: int | None = None, shares: dict | None = None) -> dict:
    """What `select` would include, plus the next `n` papers it would pull in as slots free up. Writes nothing:
    screen the off-topic ones in one pass instead of select -> screen -> select rounds."""
    tmp = Review(rv.path, json.loads(json.dumps(rv.meta)), json.loads(json.dumps(rv.tables)))
    out = select_papers(tmp, now_year=now_year, shares=shares)
    P = tmp.tables["papers"]
    brief = lambda p: {"id": p["id"], "year": p.get("year"), "title": p.get("title"), "reason": p.get("status_reason")}
    inc = [brief(p) for p in P.values() if p["status"] == "included"]
    nxt = [p for p in P.values() if p["status"] == "excluded" and not p.get("manual")
           and str(p.get("status_reason") or "").startswith(("over cap", "over seminal cap"))]
    rank = lambda p: int(re.search(r"rank (\d+)/", p["status_reason"]).group(1))
    nxt.sort(key=lambda p: (not p["status_reason"].startswith("over cap"), rank(p)))
    return {"cap": out["cap"], "included": inc, "next": [brief(p) for p in nxt[:n]], **({"shares": out["shares"]} if "shares" in out else {})}


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def screen(rv: Review, pids: str, decision: str, reason: str) -> list[dict] | dict:
    """Record a manual screening decision; `select` will not override it. `pids` may be comma-separated."""
    if decision not in ("included", "excluded", "candidate"):
        raise SystemExit("decision must be included/excluded/candidate")
    if not reason:
        raise SystemExit("a reason is required for manual screening")
    ids = [x.strip() for x in pids.split(",") if x.strip()]
    missing = [x for x in ids if x not in rv.tables["papers"]]
    if missing:
        raise SystemExit(f"not in papers: {', '.join(missing)}")
    for pid in ids:
        p = rv.tables["papers"][pid]
        p["status"], p["status_reason"], p["manual"] = decision, f"manual: {reason}", True
        p["manual_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()   # merge-screening applies only batches written after this
    rv.derive()
    out = [{"paper": pid, "status": decision} for pid in ids]
    return out if "," in pids else out[0]


def delete_record(rv: Review, table: str, id_: str) -> dict:
    """Remove a record and every reference to it. IDs are never reused."""
    T = rv.tables
    if table in LIST_TABLES or id_ not in T[table]:
        raise SystemExit(f"{id_} not in {table}")
    rep = {"removed_results": 0, "removed_links": 0, "unlinked": 0}
    del T[table][id_]
    if table == "papers":
        for c in T["clusters"].values():
            c["members"] = [m for m in c.get("members") or [] if m.get("paper_id") != id_]
    if table == "clusters":
        for xid in [x["id"] for x in T["explainers"].values() if x.get("cluster_id") == id_]:
            del T["explainers"][xid]
            rep["removed_explainers"] = rep.get("removed_explainers", 0) + 1
    key = {"papers": "paper_id", "datasets": "dataset_id", "benchmarks": "benchmark_id", "metrics": "metric_id", "tasks": "task_id"}.get(table)
    if key:
        for rid in [r["id"] for r in T["results"].values() if r.get(key) == id_]:
            del T["results"][rid]
            rep["removed_results"] += 1
    list_key = {"tasks": "task_ids", "datasets": "dataset_ids", "benchmarks": "benchmark_ids", "metrics": "metric_ids", "papers": "paper_ids",
                "search_runs": "run_ids", "search_terms": "term_ids"}.get(table)
    if table in ("papers", "search_runs"):
        fk = "paper_id" if table == "papers" else "run_id"
        before = len(T["discovery_links"])
        T["discovery_links"] = [l for l in T["discovery_links"] if l[fk] != id_]
        rep["removed_links"] = before - len(T["discovery_links"])
    for t in TABLES:
        recs = T[t] if t in LIST_TABLES else list(T[t].values())
        for r in recs:
            for k, v in list(r.items()):
                if isinstance(v, list) and id_ in v and (k == list_key or k.endswith("_ids")):
                    r[k] = [x for x in v if x != id_]
                    rep["unlinked"] += 1
                elif v == id_ and k.endswith("_id"):
                    r[k] = None
                    rep["unlinked"] += 1
    rv.derive()
    return rep


def _generic_alias(alias: str, target_names: list) -> bool:
    """True when `alias` is a strict part of a longer target name ('TREC DL' of 'TREC DL 2019+2020 (pooled)'): as an
    alias it would route every later paper that says only 'TREC DL' onto the specific record."""
    a_tok = norm_title(alias).split()
    a_norm = norm_name(alias)
    for t in target_names:
        t_tok, t_norm = norm_title(t).split(), norm_name(t)
        if not a_norm or a_norm == t_norm:
            continue
        if a_norm in t_norm or (a_tok and set(a_tok) < set(t_tok)):
            return True
    return False


def fold(rv: Review, table: str, from_id: str, into_id: str, variant_from_name: bool = False, keep_alias: bool = False) -> dict:
    """Merge a candidate dataset/benchmark/metric into an existing one: repoint every result and link, keep the old
    name (and its aliases) as aliases of the target so later `enrich`/`merge-details` runs land on it, then delete it.
    A name that is only a generic part of the target's name or aliases is not kept as an alias unless keep_alias.
    variant_from_name: rows with no metric_variant get the folded metric's name as their variant (e.g. 'AUC@5')."""
    if table not in ("datasets", "benchmarks", "metrics"):
        raise SystemExit("fold works on datasets, benchmarks or metrics")
    T = rv.tables[table]
    if from_id == into_id or from_id not in T or into_id not in T:
        raise SystemExit(f"need two different ids in {table} (got {from_id} -> {into_id})")
    src, dst = T[from_id], T[into_id]
    key = {"datasets": "dataset_id", "benchmarks": "benchmark_id", "metrics": "metric_id"}[table]
    rep = {"repointed": 0, "aliases_added": [], "aliases_skipped": []}
    for r in rv.tables["results"].values():
        if r.get(key) == from_id:
            r[key] = into_id
            if variant_from_name and table == "metrics" and not r.get("metric_variant"):
                r["metric_variant"] = src.get("name")
            rep["repointed"] += 1
    for a in [src.get("name")] + list(src.get("aliases") or []):
        if a and norm_name(a) != norm_name(dst.get("name")) and all(norm_name(a) != norm_name(x) for x in dst.setdefault("aliases", [])):
            if not keep_alias and _generic_alias(a, [dst.get("name")] + dst["aliases"]):
                rep["aliases_skipped"].append(a)
                continue
            dst["aliases"].append(a)
            rep["aliases_added"].append(a)
    if rep["aliases_skipped"]:
        rep["note"] = (f"not kept as aliases of {into_id} (generic part of its name; would misroute later papers): "
                       f"{', '.join(rep['aliases_skipped'])}. Rerun with --keep-alias to force.")
    lk = key[:-3] + "_ids"
    for t in TABLES:
        recs = rv.tables[t] if t in LIST_TABLES else list(rv.tables[t].values())
        for r in recs:
            v = r.get(lk)
            if isinstance(v, list) and from_id in v:
                r[lk] = [into_id if x == from_id else x for x in v]
                r[lk] = [x for i, x in enumerate(r[lk]) if x not in r[lk][:i]]
    delete_record(rv, table, from_id)
    return rep


def _validate_step6(rv: Review, errors: list, warnings: list) -> None:
    T = rv.tables
    papers, clusters, explainers = T["papers"], T["clusters"], T["explainers"]
    for c in clusters.values():
        cid = c["id"]
        if is_evidence(c.get("definition")):
            check_evidence(f"{cid}.definition", c["definition"], errors)
        else:
            errors.append(f"{cid}.definition: required evidence note")
        for m in c.get("members") or []:
            if m.get("paper_id") not in papers:
                errors.append(f"{cid}.members: dangling paper {m.get('paper_id')}")
            if not m.get("rationale"):
                errors.append(f"{cid}.members[{m.get('paper_id')}]: rationale required")
    for x in explainers.values():
        xid = x["id"]
        if x.get("cluster_id") not in clusters:
            errors.append(f"{xid}.cluster_id: dangling reference {x.get('cluster_id')}")
        if x.get("paper_id") not in papers:
            errors.append(f"{xid}.paper_id: dangling reference {x.get('paper_id')}")
        for f in EXPLAINER_FIELDS:
            if is_evidence(x.get(f)):
                check_evidence(f"{xid}.{f}", x[f], errors)
                if f != "vs_siblings" and x[f].get("status") in POSITIVE and x[f]["status"] != "author-reported":
                    errors.append(f"{xid}.{f}: must be author-reported (a quote from the winner's text), got {x[f]['status']}")
            else:
                errors.append(f"{xid}.{f}: required evidence note")
        for k, e in (x.get("details") or {}).items():
            check_evidence(f"{xid}.details.{k}", e, errors)
        if x.get("diagram") is not None and method_diagram(x["diagram"])[1]:
            errors.append(f"{xid}.diagram: {method_diagram(x['diagram'])[1]} - rerun merge-explainers")
        for k, e in (x.get("facets") or {}).items():
            if is_evidence(e):
                check_evidence(f"{xid}.facets.{k}", e, errors)
                if e.get("status") in POSITIVE and e["status"] != "author-reported":
                    errors.append(f"{xid}.facets.{k}: must be author-reported (a quote from the winner's text), got {e['status']}")
        fig = x.get("figure")
        if fig:
            pf = (papers.get(x.get("paper_id") or "") or {}).get("figures")
            labels = [g["label"] for g in pf["value"]] if is_evidence(pf) and pf.get("status") in POSITIVE else []
            if fig.get("label") not in labels:
                errors.append(f"{xid}.figure: label {fig.get('label')!r} not in {x.get('paper_id')}.figures")
        words = sum(len(str(x[f]["value"]).split()) for f in EXPLAINER_FIELDS if is_evidence(x.get(f)) and x[f].get("value") is not None)
        if words > 120:
            warnings.append(f"{xid}: {words} words across the four text fields (budget 120)")
        if x.get("headline") and len(str(x["headline"]).split()) > 12:
            warnings.append(f"{xid}: headline longer than 12 words")
        dl, wp = x.get("declared_limitations") or [], x.get("weak_points") or []
        if not dl or not wp:
            errors.append(f"{xid}: no critique (declared limitations and weak points of {x.get('paper_id')}): "
                          f"run the winner-critique subagent, then merge-explainers")
        for i, e in enumerate(dl):
            if check_evidence(f"{xid}.declared_limitations[{i}]", e, errors) and e["status"] in POSITIVE and e["status"] != "author-reported":
                errors.append(f"{xid}.declared_limitations[{i}]: must be author-reported (a quote from the winner's text), got {e['status']}")
        for i, e in enumerate(wp):
            if check_evidence(f"{xid}.weak_points[{i}]", e, errors) and e["status"] != "inferred":
                errors.append(f"{xid}.weak_points[{i}]: must be inferred (the reader's judgement, with a rationale), got {e['status']}")
        if len(wp) == 1:
            warnings.append(f"{xid}: 1 weak point (expected 2-5)")
    K = rv.meta.get("kpis") or []
    if not K:
        return
    if len(K) > KPI_MAX:
        errors.append(f"KPI: {len(K)} KPIs; max {KPI_MAX}")
    for k in K:
        kid = k.get("id")
        if bool(k.get("benchmark_id")) == bool(k.get("dataset_id")):
            errors.append(f"KPI {kid}: exactly one of benchmark_id / dataset_id")
        if k.get("benchmark_id") and k["benchmark_id"] not in T["benchmarks"]:
            errors.append(f"KPI {kid}: dangling benchmark {k['benchmark_id']}")
        if k.get("dataset_id") and k["dataset_id"] not in T["datasets"]:
            errors.append(f"KPI {kid}: dangling dataset {k['dataset_id']}")
        m = T["metrics"].get(k.get("metric_id") or "")
        if m is None:
            errors.append(f"KPI {kid}: dangling metric {k.get('metric_id')}")
        elif m.get("higher_is_better") not in (True, False):
            errors.append(f"KPI {kid}: metric {m['id']} has no direction (higher_is_better is null)")
        n = kpi_coverage(rv, k)
        if n < KPI_MIN_PAPERS:
            errors.append(f"KPI {kid}: covers {n} included papers; needs at least {KPI_MIN_PAPERS}")
    if not clusters:
        warnings.append("step 6 incomplete: KPIs are set but there are no clusters (cluster-input -> subagent -> merge-clusters)")
        return
    top = [r["paper_id"] for r in rank_papers(rv)["ranked"]]
    for pid in top:
        n = sum(1 for c in clusters.values() if pid in (c.get("paper_ids") or []))
        if n == 0:
            errors.append(f"{pid}: in the top {len(top)} but in no cluster (stale clusters: rerun step 6)")
        elif n > 1:
            errors.append(f"{pid}: in {n} clusters; each top paper belongs to exactly one")
    for c in clusters.values():
        for pid in c.get("paper_ids") or []:
            if pid not in top:
                errors.append(f"{c['id']}: member {pid} is not in the current top {len(top)} (stale clusters: rerun step 6)")
        xs = [x for x in explainers.values() if x.get("cluster_id") == c["id"]]
        if len(xs) != 1:
            errors.append(f"{c['id']}: needs exactly one explainer, has {len(xs)}")
        elif xs[0].get("paper_id") != c.get("winner_paper_id"):
            errors.append(f"{xs[0]['id']}: explains {xs[0].get('paper_id')} but {c['id']}'s winner is {c.get('winner_paper_id')}")
    syn = rv.meta.get("synthesis")
    if syn:
        for i, t in enumerate(syn.get("takeaways") or [], 1):
            if not t.get("evidence"):
                errors.append(f"synthesis takeaway {i}: needs at least one evidence quote")
            for e in t.get("evidence") or []:
                if e.get("paper_id") not in papers:
                    errors.append(f"synthesis takeaway {i}: dangling paper {e.get('paper_id')}")
        if clusters:
            warnings += synthesis_count_issues(rv, syn.get("takeaways") or [])
    elif explainers and len(explainers) == len(clusters):
        warnings.append("review synthesis missing: run synthesis-input -> subagent -> merge-synthesis")
    miss = _missing_member_limits(rv)
    if miss:
        warnings.append(f"stated limitations not extracted for {len(miss)} family member(s): {', '.join(miss)} "
                        f"(explainer-input C### again for its cluster, then the member-limitations subagent per member, then merge-explainers)")
    lim = rv.meta.get("limitations")
    if lim:
        need = min(2, len(clusters))
        for t in lim.get("problems") or []:
            lid, cells = t.get("id"), t.get("cells") or {}
            for cid, cell in cells.items():
                if cid not in clusters:
                    errors.append(f"limitations {lid}: dangling family {cid} (stale: rerun limitations-input -> subagent -> merge-limitations)")
                    continue
                if cell.get("state") == "not-shown":
                    continue
                if cell.get("paper_id") not in (clusters[cid].get("paper_ids") or []):
                    errors.append(f"limitations {lid}.{cid}: paper {cell.get('paper_id')} is not a member of {cid} (stale: rerun merge-limitations)")
                if cell.get("basis") == "author-reported" and not cell.get("quote"):
                    errors.append(f"limitations {lid}.{cid}: author-reported cell needs a quote")
                if cell.get("basis") == "inferred" and not cell.get("rationale"):
                    errors.append(f"limitations {lid}.{cid}: inferred cell needs a rationale")
            if sum(1 for c in cells.values() if c.get("state") == "affected") < need:
                errors.append(f"limitations {lid}: fewer than {need} affected families")
            if set(clusters) - set(cells):
                warnings.append(f"limitations {lid}: no cell for {', '.join(sorted(set(clusters) - set(cells)))} (stale: rerun merge-limitations)")
        for i, o in enumerate(lim.get("open_problems") or []):
            if o.get("basis") != "outside-knowledge":
                errors.append(f"limitations open_problems[{i}]: must carry basis outside-knowledge")
    elif rv.meta.get("synthesis") and explainers and len(explainers) == len(clusters):
        warnings.append("limitations section missing: run limitations-input -> subagent -> merge-limitations")
    if explainers and len(explainers) == len(clusters):
        warnings += report_warnings(rv, verdict=False)          # the hub's Report tab: the ten sections
    singles = sum(1 for c in clusters.values() if len(c.get("paper_ids") or []) == 1)
    if top and singles > len(top) / 2:
        warnings.append(f"step 6: {singles} single-paper clusters for {len(top)} papers - check clustering granularity")


def validate_review(rv: Review) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    T = rv.tables
    papers, tasks, datasets, benchmarks, metrics, results = T["papers"], T["tasks"], T["datasets"], T["benchmarks"], T["metrics"], T["results"]

    def fk(owner: str, field: str, ids, table: dict):
        for i in ids or []:
            if i not in table:
                errors.append(f"{owner}.{field}: dangling reference {i}")

    def ev_fields(owner: str, rec: dict):
        for k, v in rec.items():
            if is_evidence(v):
                check_evidence(f"{owner}.{k}", v, errors)

    if not rv.meta.get("scope") and str(rv.meta.get("created_at") or "") >= SCOPE_REQUIRED_FROM:
        warnings.append("no scope agreement recorded: run `scope --in ... --out ...` at step 0 (what is in, what is out, protocol rules)")
    for item in (rv.meta.get("scope") or {}).get("in") or []:
        m = _MIXED_SCOPE_ITEM.search(item)
        if m:
            warnings.append(f"scope in item lets general methods compete with targeted ones ('{m.group(0)}'): select ranks "
                            "by citations, so the most-cited general methods fill the cap; put the target in the item and "
                            "the general methods in an out item (`scope` again)")
    for d in rv.meta.get("decisions") or []:
        if not d.get("reason"):
            errors.append(f"{d.get('id')}: decision without a reason")
    warnings += adjacent_warnings(rv) + screening_warnings(rv)
    old_style = lambda s: re.search(r"\((\d+) refs; fetched (\d+)\)", s.get("query") or "")
    rewalked = {s.get("seed_paper_id") for s in T["search_runs"].values() if s["kind"] == "reference" and not old_style(s)
                and (s.get("retrieved") or 0) > 0}
    for s in T["search_runs"].values():
        m = old_style(s)
        if s["kind"] == "reference" and m and int(m.group(2)) < int(m.group(1)) and s.get("seed_paper_id") not in rewalked:
            warnings.append(f"{s['id']}: reference walk from {s.get('seed_paper_id')} kept the first {m.group(2)} of {m.group(1)} "
                            f"references in OpenAlex order (oldest first; before v1.12.1) - rerun `references {s.get('seed_paper_id')}`")
    walked_ok = {s.get("seed_paper_id") for s in T["search_runs"].values() if s["kind"] == "reference" and (s.get("retrieved") or 0) > 0}
    for s in T["search_runs"].values():
        if s["kind"] == "reference" and not s.get("retrieved") and s.get("seed_paper_id") not in walked_ok:
            warnings.append(f"{s['id']}: reference walk from {s.get('seed_paper_id')} found 0 references ({s.get('limitations') or 'no reason recorded'}) "
                            f"- rerun `references {s.get('seed_paper_id')}`: it parses the bibliography of the cached full text when OpenAlex has none")
    for p in papers.values():
        pid = p["id"]
        fk(pid, "task_ids", p.get("task_ids"), tasks)
        fk(pid, "dataset_ids", p.get("dataset_ids"), datasets)
        fk(pid, "benchmark_ids", p.get("benchmark_ids"), benchmarks)
        fk(pid, "metric_ids", p.get("metric_ids"), metrics)
        fk(pid, "result_ids", p.get("result_ids"), results)
        fk(pid, "discovery_run_ids", p.get("discovery_run_ids"), T["search_runs"])
        ev_fields(pid, p)
        if p.get("status") == "included" and not p.get("taxonomy_source") and not rv.fulltext_path(pid).exists():
            warnings.append(f"{pid}: included but no cached full text - run `fulltext {pid}` before step 5 (papers included by a later select or screen miss the first fulltext pass)")
        if p.get("status") == "included" and not p.get("taxonomy_source") and not is_evidence(p.get("published_venue")):
            warnings.append(f"{pid}: included but published venue never looked up - run `paper {pid}` (it breaks ranking ties, picks each family's example paper and adds a select bonus)")
        if p.get("status") == "included":
            for f in PAPER_REQUIRED_EVIDENCE:
                if not is_evidence(p.get(f)):
                    errors.append(f"{pid}.{f}: required for included papers - give a value or an explicit status")
            for f in ("code", "checkpoint"):
                e = p.get(f)
                if is_evidence(e) and isinstance(e.get("value"), dict):
                    av = e["value"].get("availability")
                    if av not in AVAILABILITY:
                        errors.append(f"{pid}.{f}: availability must be one of {AVAILABILITY}, got {av!r}")
                    if av == "no" and e.get("status") in ("not-found", "inaccessible"):
                        errors.append(f"{pid}.{f}: availability 'no' with status {e['status']} - not-found is not a verified no; use 'unknown'")
                elif is_evidence(e) and e.get("value") is not None:
                    errors.append(f"{pid}.{f}: value must be an object {{availability, url, kind, notes}}")
        elif p.get("status") == "excluded" and not p.get("status_reason"):
            errors.append(f"{pid}: excluded without status_reason")
        if p.get("status") not in ("included", "candidate", "excluded"):
            errors.append(f"{pid}: status must be included/candidate/excluded")
        role = p.get("paper_role") or "method"
        if role not in PAPER_ROLES:
            errors.append(f"{pid}: paper_role must be one of {PAPER_ROLES}, got {role!r}")
        elif role != "method" and not p.get("paper_role_reason"):
            errors.append(f"{pid}: paper_role {role!r} without paper_role_reason (say whose methods it evaluates)")

    inc_nonseminal = sum(1 for p in papers.values() if p["status"] == "included" and not p.get("seminal") and not p.get("taxonomy_source")
                         and not p.get("seed"))
    cap = int(rv.meta["caps"].get("max_papers", DEFAULT_MAX_PAPERS))
    if inc_nonseminal > cap:
        errors.append(f"cap exceeded: {inc_nonseminal} included non-seminal papers > max_papers {cap}")

    for t in tasks.values():
        fk(t["id"], "paper_ids", t.get("paper_ids"), papers)
        fk(t["id"], "dataset_ids", t.get("dataset_ids"), datasets)
        fk(t["id"], "metric_ids", t.get("metric_ids"), metrics)
        if t.get("parent_id"):
            fk(t["id"], "parent_id", [t["parent_id"]], tasks)
        ev_fields(t["id"], t)

    for d in datasets.values():
        fk(d["id"], "task_ids", d.get("task_ids"), tasks)
        fk(d["id"], "paper_ids", d.get("paper_ids"), papers)
        ev_fields(d["id"], d)
        if d.get("status") == "excluded":
            if not d.get("status_reason"):
                errors.append(f"{d['id']}: excluded dataset needs status_reason")
        elif d.get("status", "included") == "included":
            for f in DATASET_REQUIRED_EVIDENCE:
                if not is_evidence(d.get(f)):
                    errors.append(f"{d['id']}.{f}: required for datasets - give a value or an explicit status")
            av = d.get("availability")
            if is_evidence(av) and isinstance(av.get("value"), dict):
                a = av["value"].get("availability")
                if a not in DATASET_AVAILABILITY:
                    errors.append(f"{d['id']}.availability: must be one of {DATASET_AVAILABILITY}, got {a!r}")
                if a == "unavailable" and av.get("status") in ("not-found", "inaccessible"):
                    errors.append(f"{d['id']}.availability: 'unavailable' with status {av['status']} - not-found is not a verified no")
            lb = d.get("leaderboard")
            if is_evidence(lb) and isinstance(lb.get("value"), dict):
                ex = lb["value"].get("exists")
                if ex not in AVAILABILITY:
                    errors.append(f"{d['id']}.leaderboard: exists must be yes/no/unknown, got {ex!r}")
                if ex == "no" and lb.get("status") in ("not-found", "inaccessible"):
                    errors.append(f"{d['id']}.leaderboard: 'no' with status {lb['status']} - not-found is not a verified no")
        else:
            warnings.append(f"{d['id']} ({d.get('name')}): candidate dataset created from paper text - fill required evidence or exclude")

    for b in benchmarks.values():
        fk(b["id"], "task_ids", b.get("task_ids"), tasks)
        fk(b["id"], "dataset_ids", b.get("dataset_ids"), datasets)
        fk(b["id"], "metric_ids", b.get("metric_ids"), metrics)
        ev_fields(b["id"], b)

    for m in metrics.values():
        fk(m["id"], "task_ids", m.get("task_ids"), tasks)
        fk(m["id"], "benchmark_ids", m.get("benchmark_ids"), benchmarks)
        ev_fields(m["id"], m)
        if m.get("status") == "candidate":
            warnings.append(f"{m['id']} ({m.get('name')}): candidate metric created from paper text - define it or merge into an alias")
        elif m.get("status") == "excluded" and not m.get("status_reason"):
            errors.append(f"{m['id']}: excluded metric needs status_reason")

    for r in results.values():
        rid = r["id"]
        if r.get("paper_id") not in papers:
            errors.append(f"{rid}.paper_id: dangling reference {r.get('paper_id')}")
        for f, tbl in (("dataset_id", datasets), ("benchmark_id", benchmarks), ("metric_id", metrics), ("task_id", tasks)):
            if r.get(f) and r[f] not in tbl:
                errors.append(f"{rid}.{f}: dangling reference {r[f]}")
        if not r.get("metric_id"):
            errors.append(f"{rid}: metric_id required")
        if not (r.get("dataset_id") or r.get("benchmark_id")):
            errors.append(f"{rid}: dataset_id or benchmark_id required")
        if r.get("status") not in STATUSES:
            errors.append(f"{rid}: unknown status {r.get('status')!r}")
        if r.get("value") is not None and not r.get("source"):
            errors.append(f"{rid}: value without source")
        if r.get("provenance") == "leaderboard-verified":
            if not r.get("snapshot_date"):
                errors.append(f"{rid}: leaderboard-verified result requires snapshot_date")
            if r.get("rank") is not None and r.get("rank_denominator") is None:
                errors.append(f"{rid}: rank given without rank_denominator")
        elif r.get("rank") is not None and r.get("rank_denominator") is None:
            warnings.append(f"{rid}: rank without rank_denominator")

    for l in T["discovery_links"]:
        if l["run_id"] not in T["search_runs"]:
            errors.append(f"discovery_link: dangling run {l['run_id']}")
        if l["paper_id"] not in papers:
            errors.append(f"discovery_link: dangling paper {l['paper_id']}")
        if l.get("discovery_type") not in DISCOVERY_TYPES:
            errors.append(f"discovery_link {l['run_id']}/{l['paper_id']}: discovery_type must be one of {DISCOVERY_TYPES}")
    for s in T["search_runs"].values():
        fk(s["id"], "term_ids", s.get("term_ids"), T["search_terms"])
        if s.get("seed_paper_id"):
            fk(s["id"], "seed_paper_id", [s["seed_paper_id"]], papers)
    for q in T["search_terms"].values():
        if q.get("category") not in TERM_CATEGORIES:
            errors.append(f"{q['id']}: category must be one of {TERM_CATEGORIES}")
        if q.get("status") not in ("pending", "searched", "redundant", "out-of-scope", "deferred"):
            errors.append(f"{q['id']}: status must be pending/searched/redundant/out-of-scope/deferred")
        if q.get("status") in ("redundant", "out-of-scope", "deferred") and not q.get("status_reason"):
            errors.append(f"{q['id']}: status {q['status']} needs status_reason")

    # near-duplicate names
    for tbl in ("tasks", "datasets", "metrics", "benchmarks"):
        seen = {}
        for e in T[tbl].values():
            k = norm_name(e.get("name"))
            if k in seen:
                warnings.append(f"{tbl}: possible duplicate names {seen[k]} and {e['id']} ({e.get('name')!r})")
            seen.setdefault(k, e["id"])

    # cached 404s on cited source URLs
    for p in papers.values():
        for f in ("code", "checkpoint"):
            e = p.get(f)
            if is_evidence(e) and isinstance(e.get("value"), dict) and e["value"].get("url"):
                cf = _cache_file(rv.cache_dir, e["value"]["url"])
                if cf.exists():
                    try:
                        st = json.loads(cf.read_text()).get("status")
                        if st and st >= 400:
                            warnings.append(f"{p['id']}.{f}: url {e['value']['url']} last fetched with HTTP {st}")
                    except Exception:
                        pass
    _validate_step6(rv, errors, warnings)
    return errors, warnings


# --------------------------------------------------------------------------
# merge step-5 subagent outputs
# --------------------------------------------------------------------------

def _settle_note(e, text: str, src: str, method: str, abstract_only: bool, report: dict, field: str) -> dict:
    """Turn an extractor's note into a checked evidence note: author-reported quotes must be in the cached text."""
    if not isinstance(e, dict):
        return evidence(None, "not-found", rationale=f"malformed detail for {field}")
    st = e.get("status", "author-reported" if e.get("value") is not None else "not-reported")
    if st not in STATUSES:
        st = "author-reported" if e.get("value") is not None else "not-reported"
    if e.get("value") is not None and st in POSITIVE:
        q = e.get("quote")
        if st == "inferred":
            return evidence(e["value"], "inferred", source=src, checked=today(), location=e.get("location"),
                            quote=q, rationale=e.get("rationale") or "inferred by extractor (no rationale given)")
        if quote_in_text(q, text):
            return evidence(e["value"], "author-reported", source=src, checked=today(), location=e.get("location"), quote=q)
        report["downgraded"] += 1
        return evidence(None, "not-found", checked=today(), location=e.get("location"),
                        rationale=f"quote not found verbatim in cached text ({method}); claimed value {e['value']!r} dropped; quote: {q!r}")
    if abstract_only and st == "not-reported":
        return evidence(None, "inaccessible", checked=today(), rationale="only the abstract was accessible; silence in an abstract is not 'not reported'")
    return evidence(None, st if st in NEGATIVE else "not-found", checked=today(), rationale=e.get("rationale"))


def _generic_part_of(rv: "Review", table: str, name: str) -> list[str]:
    """Names of the 2+ non-excluded records that `name` is a whole-word prefix of ('TREC DL' -> 'TREC DL 2019',
    'TREC DL 2020'), else []. Such a name is ambiguous: linking it to one record or minting a candidate are both wrong."""
    w = norm_title(name).split()
    if not w:
        return []
    hits = [e["name"] for e in rv.tables[table].values() if e.get("status") != "excluded"
            and len(norm_title(e.get("name")).split()) > len(w) and norm_title(e.get("name")).split()[:len(w)] == w]
    return hits if len(hits) >= 2 else []


def merge_details(rv: Review, replace: list | None = None) -> dict:
    """Merge data/details/*.json. A merge only adds rows (a row already merged is skipped), so a paper whose text
    changed (`fulltext --published`) and was extracted again is listed in `replace`: its own step-5 rows and its
    dataset, metric and benchmark links go first (archived leaderboard rows stay). Meerkat's re-read kept its 98 arXiv-v2
    rows beside the 58 new ones until this existed (llm-to-domain-slm, 2026-10-26)."""
    report = {"merged": 0, "downgraded": 0, "duplicates_skipped": 0, "skipped": [], "warnings": [], "missing_details": []}
    ddir = rv.path / "data" / "details"
    for pid in replace or []:
        if pid not in rv.tables["papers"] or not (ddir / f"{pid}.json").exists():
            raise SystemExit(f"--replace {pid}: no such paper or no data/details/{pid}.json to merge in place of its rows")
        gone = [rid for rid, r in rv.tables["results"].items() if r.get("paper_id") == pid and r.get("provenance") != "leaderboard-verified"]
        for rid in gone:
            del rv.tables["results"][rid]
        p = rv.tables["papers"][pid]
        p["result_ids"] = [r for r in p.get("result_ids") or [] if r not in set(gone)]
        for k in ("dataset_ids", "metric_ids", "benchmark_ids"):
            p[k] = []
        report.setdefault("replaced", {})[pid] = len(gone)
    report["missing_details"] = [p["id"] for p in sorted(rv.tables["papers"].values(), key=lambda p: p["id"])
                                 if needs_details(p) and not (ddir / f"{p['id']}.json").exists()]
    if report["missing_details"]:
        report["warnings"].insert(0, f"{len(report['missing_details'])} included paper(s) have NO details file (step-5 agent died or "
                                     f"never ran) - their method notes stay empty: {', '.join(report['missing_details'])}")
    if not ddir.exists():
        return report
    for f in sorted(ddir.glob("*.json")):
        try:
            det = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            report["skipped"].append(f"{f.name}: invalid JSON ({e})")
            continue
        pid = det.get("paper_id") or f.stem
        p = rv.tables["papers"].get(pid)
        if p is None:
            report["skipped"].append(f"{f.name}: unknown paper {pid}")
            continue
        if p.get("status") != "included":
            report["skipped"].append(f"{f.name}: paper {pid} is {p.get('status')}, not included")
            continue
        text, fmeta = rv.read_fulltext(pid)
        text = text or ""
        src = fmeta.get("url") or str(rv.fulltext_path(pid))
        method = fmeta.get("method", "unknown")
        abstract_only = method == "abstract-only" or not text

        def settle(field: str, e: dict) -> dict:
            return _settle_note(e, text, src, method, abstract_only, report, field)

        for field in DETAIL_FIELDS:
            if field in det:   # emphasis: the highlights step 6 adds to the notes of a slide's best paper (merge-emphasis)
                p[field] = _set_emphasis(settle(field, det[field]), det[field], report, f"{pid} {field}")
            elif is_evidence(p.get(field)):
                pass
            else:
                p[field] = evidence(None, "inaccessible" if abstract_only else "not-reported", checked=today(),
                                    rationale="extractor returned no entry for this field")
        if det.get("evidence_limitations"):
            p["evidence_limitations"] = det["evidence_limitations"]
        elif abstract_only and not p.get("evidence_limitations"):
            p["evidence_limitations"] = "full text not accessible; details limited to the abstract"

        for du in det.get("datasets_used") or []:
            name = (du or {}).get("name")
            if not name or is_placeholder_name(name):
                if name:
                    report["warnings"].append(f"{pid}: datasets_used names a placeholder ({name!r}) - skipped")
                continue
            d = rv._find_by_name("datasets", name)
            if d is None:
                amb = _generic_part_of(rv, "datasets", name)
                if amb:   # 'TREC DL' next to 'TREC DL 2019' / 'TREC DL 2020': ambiguous, not new; a candidate came back on every merge
                    report["warnings"].append(f"{pid}: dataset {name!r} is ambiguous (prefix of {', '.join(amb[:4])}) - link skipped, no candidate created")
                    continue
                d = rv.add_dataset(name, status="candidate")
                report["warnings"].append(f"{pid}: dataset {name!r} not in datasets table - created {d['id']} as candidate")
            if quote_in_text(du.get("quote"), text) or not du.get("quote"):
                if d["id"] not in p["dataset_ids"]:
                    p["dataset_ids"].append(d["id"])
            else:
                report["warnings"].append(f"{pid}: dataset {name!r} quote not found in text - link skipped")

        added_here: set[str] = set()
        for r in det.get("results") or []:
            if not isinstance(r, dict):
                continue
            dname, mname = r.get("dataset") or r.get("benchmark"), r.get("metric")
            if (dname and is_placeholder_name(dname)) or (mname and is_placeholder_name(mname)):
                report["warnings"].append(f"{pid}: result row names a placeholder dataset/metric ({dname!r} / {mname!r}) - skipped, no candidate created")
                continue
            if not mname or not dname:
                report["warnings"].append(f"{pid}: result row missing dataset/metric - skipped: {r}")
                continue
            if not quote_in_text(r.get("quote"), text):
                report["downgraded"] += 0  # counted as a warning, not a field downgrade
                report["warnings"].append(f"{pid}: result quote not found in text - row skipped: {r.get('quote')!r}")
                continue
            # Checked before any lookup creates a candidate: a re-merge must not recreate a dataset/metric that was
            # deleted or folded (dashcam: 'Recall at 95% precision' came back on every merge).
            # A row already merged and later repointed (fold, `import --table results` onto a benchmark) no longer matches
            # on dataset/benchmark/metric; it is the same table cell when location, quote, variants and value all match.
            if r.get("location") and r.get("quote") and any(
                    x["paper_id"] == pid and x.get("provenance") == "paper-reported" and x["id"] not in added_here
                    and x.get("location") == r.get("location") and x.get("quote") == r.get("quote")
                    and x.get("method_variant") == r.get("method_variant") and x.get("metric_variant") == r.get("metric_variant")
                    and _num(x.get("value")) == _num(r.get("value")) for x in rv.tables["results"].values()):
                continue
            d = rv._find_by_name("datasets", dname)
            bname = r.get("benchmark")
            bid = re.match(r"\s*(B\d{3,})\b", bname or "")   # "B009", or "B009 CVUSA retrieval" copied from an id+name list
            b = (rv.tables["benchmarks"].get(bid.group(1) if bid else bname.strip()) or rv._find_by_name("benchmarks", bname)) if bname \
                else rv._find_by_name("benchmarks", dname)   # an id, an id followed by its name, or a name/alias
            if bname and b is None:
                report["warnings"].append(f"{pid}: benchmark {bname!r} not in benchmarks - row kept on its dataset only")
            if d is None and b is None:
                d = rv.add_dataset(dname, status="candidate")
                report["warnings"].append(f"{pid}: dataset {dname!r} not in datasets table - created {d['id']} as candidate")
            m = rv._find_by_name("metrics", mname)
            if m is None:
                m = rv.add_metric(mname, status="candidate")
                report["warnings"].append(f"{pid}: metric {mname!r} not in metrics table - created {m['id']} as candidate")
            # a row is the same row only if every field that tells two numbers apart matches (metric_variant and
            # protocol included: AUC@5 vs AUC@10, RANSAC vs LO-RANSAC). Value stays out of the key so a re-run is idempotent.
            dup = next((x for x in rv.tables["results"].values() if x["paper_id"] == pid and x.get("dataset_id") == (d or {}).get("id")
                        and x.get("benchmark_id") == (b or {}).get("id") and x["metric_id"] == m["id"] and x.get("split") == r.get("split")
                        and x.get("method_variant") == r.get("method_variant") and x.get("metric_variant") == r.get("metric_variant")
                        and x.get("protocol") == r.get("protocol") and x.get("provenance") == "paper-reported"), None)
            if dup:
                if dup["id"] in added_here:   # same key twice in one details file; earlier-run rows are just "already merged"
                    report["duplicates_skipped"] += 1
                    same = _num(dup.get("value")) == _num(r.get("value"))
                    report["warnings"].append(f"{pid}: duplicate result row skipped ({mname} on {dname}, split {r.get('split')!r}, "
                                              f"variant {r.get('method_variant')!r}/{r.get('metric_variant')!r}, protocol {r.get('protocol')!r})"
                                              + ("" if same else f" - CONFLICTING value {r.get('value')!r} vs kept {dup.get('value')!r} ({dup['id']})"))
                continue
            new = rv.add_result(paper_id=pid, method_variant=r.get("method_variant"), checkpoint=r.get("checkpoint"),
                          dataset_id=(d or {}).get("id"), benchmark_id=(b or {}).get("id"), task_id=r.get("task_id"),
                          dataset_version=r.get("dataset_version"), split=r.get("split"), protocol=r.get("protocol"),
                          metric_id=m["id"], metric_variant=r.get("metric_variant"), value=r.get("value"), units=r.get("units"),
                          training_data=r.get("training_data"), test_time_adaptations=r.get("test_time_adaptations"),
                          compute=r.get("compute"), source=src, location=r.get("location"), quote=r.get("quote"),
                          caveats=r.get("caveats"), provenance="paper-reported", status="author-reported",
                          split_note=r.get("split_note"), row_kind=r.get("row_kind") if r.get("row_kind") in ROW_KINDS else None)
            if r.get("row_kind") is not None and r.get("row_kind") not in ROW_KINDS:
                report["warnings"].append(f"{pid}: row_kind {r.get('row_kind')!r} is not one of {', '.join(ROW_KINDS)} - "
                                          f"{new['id']} falls back to its caveats and method name")
            added_here.add(new["id"])
            if d and d["id"] not in p["dataset_ids"]:
                p["dataset_ids"].append(d["id"])
            if m["id"] not in p["metric_ids"]:
                p["metric_ids"].append(m["id"])
        report["merged"] += 1
    rv.derive()
    return report


# --------------------------------------------------------------------------
# step 6: clusters
# --------------------------------------------------------------------------

def _method_notes(p: dict) -> dict:
    return {f: {k: p[f].get(k) for k in ("value", "quote", "location")}
            for f in ["contribution"] + DETAIL_FIELDS if is_evidence(p.get(f)) and p[f].get("status") in POSITIVE}


def cluster_input(rv: Review) -> dict:
    rp = rank_papers(rv)
    if not rp["ranked"]:
        raise SystemExit("no ranked papers: set KPIs with `kpi --add`, then run `rank-papers`")
    papers = []
    for r in rp["ranked"]:
        p = rv.tables["papers"][r["paper_id"]]
        papers.append({"paper_id": p["id"], "title": p.get("title"), "year": p.get("year"), "position": r["position"],
                       "best_rank": f"{r['best_rank']}/{r['denominator']} on {r['best_kpi']}", "notes": _method_notes(p)})
    fams = []
    tdir = rv.path / "data" / "seed_taxonomy"
    for f in sorted(tdir.glob("*.json")) if tdir.exists() else []:
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for m in data.get("method_families") or []:
            fams.append({"survey": f.stem, "name": m.get("name"), "definition": m.get("definition"), "quote": m.get("quote")})
    return {"kpis": rv.meta.get("kpis"), "papers": papers, "method_families": fams, "unranked": rp["unranked"],
            "output_path": str(rv.path / "data" / "clusters" / "clusters.json")}


def merge_clusters(rv: Review) -> dict:
    f = rv.path / "data" / "clusters" / "clusters.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the clustering subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f'{f}: invalid JSON ({e}); expected {{"clusters": [...]}}; nothing changed')
    items = data.get("clusters") if isinstance(data, dict) else data
    if not isinstance(items, list) or not any(isinstance(it, dict) and str(it.get("name") or "").strip() for it in items):
        raise SystemExit(f'{f}: expected {{"clusters": [{{"name": ..., "members": [...]}}, ...]}} with at least one named cluster; nothing changed')
    rep = {"clusters": 0, "members": 0, "quotes_dropped": 0, "removed": [], "renamed": [], "warnings": [], "style": []}
    C, papers, src = rv.tables["clusters"], rv.tables["papers"], "data/clusters/clusters.json"
    by_name = {norm_name(c.get("name")): c for c in C.values()}
    keep = set()
    for it in items:
        if not isinstance(it, dict):
            rep["warnings"].append(f"cluster entry {it!r} is not an object - skipped")
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            rep["warnings"].append("cluster without a name skipped")
            continue
        c = by_name.get(norm_name(name))
        if c is None:
            # a renamed cluster keeps its id (and its explainer) when at least half its members are the same
            new_m = {m.get("paper_id") for m in it.get("members") or [] if isinstance(m, dict)}
            for old in C.values():
                old_m = set(old.get("paper_ids") or [])
                if old["id"] not in keep and old_m and len(old_m & new_m) * 2 >= max(len(old_m), len(new_m)):
                    c = old
                    rep["renamed"].append(old["id"])
                    by_name[norm_name(name)] = c
                    break
        if c is None:
            c = new_blank("clusters", rv.new_id("C"))
            C[c["id"]] = c
            by_name[norm_name(name)] = c      # a repeated name in the file updates this cluster, not a second one
        keep.add(c["id"])
        d = it.get("definition") or {}
        d = {"value": d} if isinstance(d, str) else d
        if not isinstance(d, dict):
            rep["warnings"].append(f"{name}: definition is not an object - recorded as not-found")
            d = {}
        c["name"] = name
        rep["style"] += cluster_name_lint(name)
        c["definition"] = (evidence(d["value"], "inferred", source=src, checked=today(),
                                    rationale=d.get("rationale") or "written by the clustering step from the members' method notes")
                           if d.get("value") else evidence(None, "not-found", checked=today(), rationale="clustering step gave no definition"))
        c["distinguishing_trait"], c["survey_family"] = it.get("distinguishing_trait"), it.get("survey_family")
        members = []
        for m in it.get("members") or []:
            if not isinstance(m, dict):
                rep["warnings"].append(f"{name}: member {m!r} is not an object - skipped")
                continue
            pid = m.get("paper_id")
            p = papers.get(pid or "")
            if p is None or p.get("status") != "included":
                rep["warnings"].append(f"{name}: member {pid} is not an included paper - skipped")
                continue
            if any(x["paper_id"] == pid for x in members):
                continue
            q = m.get("quote")
            if q:
                text, _ = rv.read_fulltext(pid)
                if not quote_in_text(q, text or ""):
                    rep["quotes_dropped"] += 1
                    rep["warnings"].append(f"{name}: quote for {pid} not found in its cached text - quote dropped: {q!r}")
                    q = None
            members.append({"paper_id": pid, "field": m.get("field"), "rationale": m.get("rationale"), "quote": q,
                            "location": m.get("location") if q else None})
        c["members"] = members
        rep["clusters"] += 1
        rep["members"] += len(members)
    for cid in [cid for cid in C if cid not in keep]:
        delete_record(rv, "clusters", cid)
        rep["removed"].append(cid)
    rv.derive()
    return rep


# --------------------------------------------------------------------------
# step 6: explainers
# --------------------------------------------------------------------------

def explainer_input(rv: Review, cid: str) -> dict:
    c = rv.tables["clusters"].get(cid)
    if c is None:
        raise SystemExit(f"{cid} not in clusters")
    w = c.get("winner_paper_id")
    if not w:
        raise SystemExit(f"{cid} has no winner ({c.get('winner_reason')}); rerun rank-papers / merge-clusters")
    p = rv.tables["papers"][w]
    _, fm = rv.read_fulltext(w)
    figs = p.get("figures")
    pos = figs is not None and is_evidence(figs) and figs.get("status") in POSITIVE
    rp = rank_papers(rv)
    rk = next((r for r in rp["ranked"] + rp["below_cut"] if r["paper_id"] == w), None)
    return {"cluster": {"id": cid, "name": c.get("name"), "definition": (c.get("definition") or {}).get("value"),
                        "distinguishing_trait": c.get("distinguishing_trait")},
            "winner": {"paper_id": w, "title": p.get("title"), "year": p.get("year"), "venue": venue_text(p),
                       "fulltext_path": str(rv.fulltext_path(w)),
                       "fulltext_method": fm.get("method"), "notes": _method_notes(p),
                       "figures": [{"label": g["label"], "caption": g["caption"]} for g in figs["value"]] if pos else [],
                       "figures_status": figs["status"] if is_evidence(figs) else "figures not extracted - run `figures`",
                       "kpi": rk, "availability": _availability(p), "evidence_limitations": p.get("evidence_limitations")},
            "review": {"purpose": rv.meta.get("purpose"), "scope": rv.meta.get("scope"),
                       "kpis": [{"id": k["id"], "label": _kpi_label(rv, k)} for k in rv.meta.get("kpis") or []]},
            "siblings": [{"paper_id": s, "title": rv.tables["papers"][s].get("title"), "notes": _method_notes(rv.tables["papers"][s]),
                          "fulltext_path": str(rv.fulltext_path(s)), "fulltext_method": rv.read_fulltext(s)[1].get("method"),
                          "limits_output_path": str(_member_limits_path(rv, s))}
                         for s in c.get("paper_ids") or [] if s != w and s in rv.tables["papers"]],
            "output_path": str(rv.path / "data" / "explainers" / f"{cid}.json"),
            "critique_output_path": str(rv.path / "data" / "explainers" / f"{cid}.critique.json")}


def _emphasis(phrases, text, rep: dict | None = None, where: str = "") -> list[str] | None:
    """The writer's emphasis phrases that the hub highlights in `text`: each copied verbatim from it as whole words
    (ASCII word boundaries, as the hub's hl() matches them), at most EMPHASIS_WORDS words, distinct, not the whole text;
    the first EMPHASIS_MAX survive. A bad phrase is dropped, never the text. None when the writer gave no emphasis."""
    if phrases is None or not isinstance(text, str):
        return None
    out, bad = [], []
    for p in phrases if isinstance(phrases, list) else [phrases]:
        p = p.strip() if isinstance(p, str) else ""
        if (p and p not in out and p != text.strip() and len(p.split()) <= EMPHASIS_WORDS
                and re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text, re.ASCII)):
            out.append(p)
        else:
            bad.append(p)
    if rep is not None and bad:
        rep["warnings"].append(f"{where}: emphasis {bad!r} dropped (each must be <= {EMPHASIS_WORDS} words copied verbatim from its text)")
    if rep is not None and len(out) > EMPHASIS_MAX:
        rep["warnings"].append(f"{where}: {len(out)} emphasis phrases, kept the first {EMPHASIS_MAX}")
    return out[:EMPHASIS_MAX]


def _set_emphasis(note: dict, src, rep: dict, where: str) -> dict:
    """Copy a written item's `emphasis` onto its merged note when the note kept its text."""
    em = _emphasis(src.get("emphasis") if isinstance(src, dict) else None, note.get("value"), rep, where)
    if em is not None:
        note["emphasis"] = em
    return note


# STE100 applied about 80% of the way (prompts/ste-style.md). The merges list these as `style` notes, never as
# errors; validate leaves them out because `validate --strict` stops on warnings and the style is a target, not a rule.
STE_MAX_WORDS = 25
STE_AVOID = ("via", "e.g.", "i.e.", "etc.", "utilize", "utilise", "leverage", "leverages", "whereby", "aforementioned")
CLUSTER_NAME_WORDS = (6, 12)
GLOSSARY_MAX = 10
_STE_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])")
_STE_ING = re.compile(r"(?:\b(?:is|are|was|were|be|been|by|while|when|before|after|without)\s+|,\s+)([a-z]+ing)\b")
_STE_ING_OK = {"during", "nothing", "something", "everything", "thing", "string", "according"}
_STE_CONTRACTION = re.compile(r"\b\w+(?:n't|'re|'ve|'ll)\b", re.I)


def ste_lint(text) -> list[str]:
    """Style notes for one step-6 text: sentences over STE_MAX_WORDS words, avoided words, -ing verb forms and
    contractions. -ing nouns pass (training, heading): only the progressive, by/while/when/... and comma-clause
    forms ("is matching", "by comparing", ", making") are flagged."""
    out = []
    for sent in _STE_SENT.split(str(text or "").strip()):
        n = len(sent.split())
        if n > STE_MAX_WORDS:
            out.append(f"sentence of {n} words (max {STE_MAX_WORDS}): {sent[:60]!r}")
        low = sent.lower()
        out += [f"avoid {w!r}" for w in STE_AVOID if re.search(r"(?<![\w.])" + re.escape(w) + r"(?!\w)", low)]
        out += [f"-ing verb form {m.group(0).strip(', ')!r}" for m in _STE_ING.finditer(sent) if m.group(1) not in _STE_ING_OK]
        out += [f"contraction {m.group(0)!r}" for m in _STE_CONTRACTION.finditer(sent)]
    return out


def cluster_name_lint(name) -> list[str]:
    """A family name says what its methods do and how they differ, in 6-12 words; a 2-4 word label is too short."""
    n, (lo, hi) = len(str(name or "").split()), CLUSTER_NAME_WORDS
    return [] if lo <= n <= hi else [f"cluster name {name!r} has {n} words (want {lo}-{hi} words: what the methods do and how they differ)"]


DIAGRAM_BOXES, DIAGRAM_LABEL_WORDS, DIAGRAM_KINDS = (3, 8), 6, ("input", "step", "output")


def method_diagram(d) -> tuple[dict | None, str | None]:
    """The slide's method diagram, checked: 3-8 boxes {id, label (<= 6 words), kind input|step|output} and arrows
    [from, to] between known boxes, every box on an arrow, no loop. Returns (clean diagram, None) or (None, why)."""
    if not isinstance(d, dict) or not isinstance(d.get("nodes"), list) or not isinstance(d.get("edges"), list):
        return None, "not an object with nodes and edges lists"
    lo, hi = DIAGRAM_BOXES
    if not lo <= len(d["nodes"]) <= hi:
        return None, f"{len(d['nodes'])} boxes (want {lo}-{hi} boxes)"
    nodes, ids = [], set()
    for n in d["nodes"]:
        if not isinstance(n, dict) or not str(n.get("id") or "").strip() or not str(n.get("label") or "").strip():
            return None, f"box {n!r} needs an id and a label"
        nid, label = str(n["id"]).strip(), " ".join(str(n["label"]).split())
        if nid in ids:
            return None, f"two boxes with id {nid!r}"
        if len(label.split()) > DIAGRAM_LABEL_WORDS:
            return None, f"label {label!r} has {len(label.split())} words (max {DIAGRAM_LABEL_WORDS} words)"
        ids.add(nid)
        nodes.append({"id": nid, "label": label, "kind": n.get("kind") if n.get("kind") in DIAGRAM_KINDS else "step"})
    edges = []
    for e in d["edges"]:
        if not isinstance(e, (list, tuple)) or len(e) != 2:
            return None, f"arrow {e!r} is not a [from, to] pair"
        a, b = str(e[0]).strip(), str(e[1]).strip()
        if a not in ids or b not in ids:
            return None, f"arrow {e!r} names an unknown box"
        if a == b:
            return None, f"arrow {e!r} is a loop"
        if [a, b] not in edges:
            edges.append([a, b])
    lonely = ids - {x for e in edges for x in e}
    if lonely:
        return None, f"no arrow touches box(es) {sorted(lonely)}"
    indeg = {i: 0 for i in ids}
    for a, b in edges:
        indeg[b] += 1
    ready, seen = [i for i in ids if not indeg[i]], 0
    while ready:                                  # Kahn's sort: boxes left over sit on a loop
        i = ready.pop()
        seen += 1
        for a, b in edges:
            if a == i:
                indeg[b] -= 1
                if not indeg[b]:
                    ready.append(b)
    if seen < len(ids):
        return None, "the arrows make a loop"
    return {"nodes": nodes, "edges": edges}, None


def _style(rep: dict, where: str, text) -> None:
    rep.setdefault("style", []).extend(f"{where}: {m}" for m in ste_lint(text))


def merge_explainers(rv: Review) -> dict:
    rep = {"merged": 0, "downgraded": 0, "skipped": [], "warnings": [], "style": []}
    xdir = rv.path / "data" / "explainers"
    if not xdir.exists():
        return rep
    X, C, papers = rv.tables["explainers"], rv.tables["clusters"], rv.tables["papers"]
    for f in sorted(xdir.glob("*.json")):
        if not re.fullmatch(r"C\d{3,}", f.stem):
            continue
        try:
            det = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            rep["skipped"].append(f"{f.name}: invalid JSON ({e})")
            continue
        cid = det.get("cluster_id") or f.stem
        c = C.get(cid)
        if c is None:
            rep["skipped"].append(f"{f.name}: unknown cluster {cid}")
            continue
        pid = det.get("paper_id") or c.get("winner_paper_id")
        if not pid or pid != c.get("winner_paper_id"):
            rep["skipped"].append(f"{f.name}: explains {pid} but {cid}'s winner is {c.get('winner_paper_id')} - rerun the explainer for the winner")
            continue
        text, fm = rv.read_fulltext(pid)
        text = text or ""
        src, method = fm.get("url") or str(rv.fulltext_path(pid)), fm.get("method", "unknown")
        abstract_only = method == "abstract-only" or not text
        x = next((e for e in X.values() if e.get("cluster_id") == cid), None)
        if x is None:
            x = new_blank("explainers", rv.new_id("X"))
            X[x["id"]] = x
        if x.get("paper_id") != pid:                     # a new winner: the old paper's critique no longer applies
            x["declared_limitations"], x["weak_points"] = [], []
        x.update({"cluster_id": cid, "paper_id": pid, "headline": det.get("headline"),
                  "glossary": det.get("glossary") if isinstance(det.get("glossary"), list) else []})
        details = x["details"] = {}       # the longer text behind each section's "More detail" fold
        x["diagram"] = None
        if det.get("diagram") is not None:
            x["diagram"], why = method_diagram(det["diagram"])
            if why:
                rep["warnings"].append(f"{f.name}: diagram dropped ({why})")
        _style(rep, f"{f.name} headline", det.get("headline"))
        if len(x["glossary"]) > GLOSSARY_MAX:
            rep["style"].append(f"{f.name} glossary: {len(x['glossary'])} terms (max {GLOSSARY_MAX}: keep the terms a reader needs)")
        for i, g in enumerate(x["glossary"]):
            _style(rep, f"{f.name} glossary[{i}]", g.get("plain") if isinstance(g, dict) else None)
        for fld in ("problem", "key_idea", "how_it_works"):
            e = det.get(fld) or {"value": None, "status": "not-reported"}
            if isinstance(e, dict) and e.get("status") in ("inferred", "verified"):
                e = dict(e, status="author-reported")   # slide text must quote the winner's text; no inferred bypass
            x[fld] = _set_emphasis(_settle_note(e, text, src, method, abstract_only, rep, fld), e, rep, f"{f.name} {fld}")
            _style(rep, f"{f.name} {fld}", e.get("value") if isinstance(e, dict) else None)
            dt = e.get("detail") if isinstance(e, dict) else None
            dt = {"value": dt} if isinstance(dt, str) else dt
            if isinstance(dt, dict) and dt.get("value"):
                dt = dict(dt, status="author-reported")  # a detail quotes the winner's text like its section
                details[fld] = _settle_note(dt, text, src, method, abstract_only, rep, f"{fld}.detail")
                _style(rep, f"{f.name} {fld}.detail", dt["value"])
        siblings = [s for s in c.get("paper_ids") or [] if s != pid]
        if not siblings:
            x["vs_siblings"] = evidence(None, "not-applicable", checked=today(), rationale="single-paper cluster")
        else:
            vs = det.get("vs_siblings") or {}
            vs = {"value": vs} if isinstance(vs, str) else vs
            vs = vs if isinstance(vs, dict) else {}
            x["vs_siblings"] = _set_emphasis(evidence(vs["value"], "inferred", source=src, checked=today(),
                                                      rationale=vs.get("rationale") or f"compared with {', '.join(siblings)} on their method notes")
                                             if vs.get("value") else evidence(None, "not-found", checked=today(), rationale="explainer step gave no comparison"),
                                             vs, rep, f"{f.name} vs_siblings")
            _style(rep, f"{f.name} vs_siblings", vs.get("value"))
            dt = vs.get("detail")
            dt = {"value": dt} if isinstance(dt, str) else dt
            if vs.get("value") and isinstance(dt, dict) and dt.get("value"):
                details["vs_siblings"] = evidence(dt["value"], "inferred", source=src, checked=today(),
                                                  rationale=dt.get("rationale") or x["vs_siblings"]["rationale"])
                _style(rep, f"{f.name} vs_siblings.detail", dt["value"])
        facets = det.get("facets") if isinstance(det.get("facets"), dict) else {}
        x["facets"] = {}
        for k in FACETS:
            e = facets.get(k)
            if e is None:
                continue
            if isinstance(e, dict) and e.get("status") in ("inferred", "verified"):
                e = dict(e, status="author-reported")
            x["facets"][k] = _settle_note(e, text, src, method, abstract_only, rep, f"facets.{k}")
        fig = det.get("figure")
        if fig is not None and not isinstance(fig, dict):
            rep["warnings"].append(f"{f.name}: figure must be an object {{label, reason}}, got {fig!r} - figure dropped")
            fig = None
        pf = papers[pid].get("figures")
        labels = [g["label"] for g in pf["value"]] if is_evidence(pf) and pf.get("status") in POSITIVE else []
        if fig and fig.get("label") in labels:
            x["figure"] = {"label": fig["label"], "reason": fig.get("reason")}
        else:
            if fig:
                rep["warnings"].append(f"{f.name}: figure {fig.get('label')!r} not in {pid}.figures - figure dropped")
            x["figure"] = None
        rep["merged"] += 1
    rep["critiques"] = 0
    for f in sorted(xdir.glob("C*.critique.json")):
        _merge_critique(rv, f, rep)
    rep["member_limits"] = 0
    for x in X.values():
        if x.get("cluster_id") in C and x.get("paper_id") == C[x["cluster_id"]].get("winner_paper_id"):
            _merge_member_limits(rv, x, C[x["cluster_id"]], rep)
    rv.derive()
    return rep


def _declared_limits(items, text: str, src: str, method: str, abstract_only: bool, rep: dict, where: str) -> list[dict]:
    """Limitations a paper states, each settled against its cached text: an author-reported note whose quote is in the
    text, else one negative note (not-reported when the writer searched and found none, not-found when every quote failed)."""
    dl, negative = [], None
    for i, e in enumerate(items or []):
        if isinstance(e, dict) and e.get("status") in ("inferred", "verified"):
            e = dict(e, status="author-reported")        # a declared limitation is the authors' own words, so it must quote them
        n = _settle_note(e, text, src, method, abstract_only, rep, f"{where} declared_limitations[{i}]")
        _style(rep, f"{where} declared_limitations[{i}]", e.get("value") if isinstance(e, dict) else None)
        if n["status"] in POSITIVE:
            dl.append(_set_emphasis(n, e, rep, f"{where} declared_limitations[{i}]"))
        elif n["status"] == "not-found":
            rep["warnings"].append(f"{where}: declared_limitations[{i}] dropped ({n.get('rationale')})")
        else:
            negative = n
    return dl or [negative or evidence(None, "not-found", checked=today(),
                                       rationale="no declared limitation whose quote is in the cached text")]


def _member_limits_path(rv: Review, pid: str) -> Path:
    return rv.path / "data" / "explainers" / "limits" / f"{pid}.json"


def _merge_member_limits(rv: Review, x: dict, c: dict, rep: dict) -> None:
    """The limitations each non-winner member of the cluster states in its own text (data/explainers/limits/<P-id>.json,
    prompts/member-limitations.md), settled like the winner's declared limitations. Only current members are kept;
    a member with no file has no entry, which `status` and `validate` report."""
    out = {}
    for pid in [s for s in c.get("paper_ids") or [] if s != c.get("winner_paper_id") and s in rv.tables["papers"]]:
        f = _member_limits_path(rv, pid)
        if not f.exists():
            continue
        try:
            det = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            rep["skipped"].append(f"limits/{f.name}: invalid JSON ({e})")
            continue
        if not isinstance(det, dict) or det.get("paper_id", pid) != pid:
            rep["skipped"].append(f"limits/{f.name}: not an object for {pid}")
            continue
        text, fm = rv.read_fulltext(pid)
        text = text or ""
        method = fm.get("method", "unknown")
        out[pid] = _declared_limits(det.get("declared_limitations"), text, fm.get("url") or str(rv.fulltext_path(pid)),
                                    method, method == "abstract-only" or not text, rep, f"limits/{f.name}")
        rep["member_limits"] = rep.get("member_limits", 0) + 1
    x["member_limitations"] = out


def _merge_critique(rv: Review, f: Path, rep: dict) -> None:
    """A winner's critique: limitations the paper states (verbatim, author-reported) and weak points the reader found
    (inferred, each with a rationale; a quote it rests on is optional, but one that is not in the text is dropped)."""
    try:
        det = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        rep["skipped"].append(f"{f.name}: invalid JSON ({e})")
        return
    cid = det.get("cluster_id") or f.name.split(".")[0]
    c = rv.tables["clusters"].get(cid)
    x = next((e for e in rv.tables["explainers"].values() if e.get("cluster_id") == cid), None)
    pid = det.get("paper_id")
    if c is None or x is None:
        rep["skipped"].append(f"{f.name}: {cid} has no {'cluster' if c is None else 'explainer'} - merge the slide first")
        return
    if pid != c.get("winner_paper_id") or pid != x.get("paper_id"):
        rep["skipped"].append(f"{f.name}: critiques {pid} but {cid}'s winner is {c.get('winner_paper_id')} - rerun the critique for the winner")
        return
    text, fm = rv.read_fulltext(pid)
    text = text or ""
    src, method = fm.get("url") or str(rv.fulltext_path(pid)), fm.get("method", "unknown")
    abstract_only = method == "abstract-only" or not text
    dl = _declared_limits(det.get("declared_limitations"), text, src, method, abstract_only, rep, f.name)
    wp = []
    for i, e in enumerate(det.get("weak_points") or []):
        if not isinstance(e, dict) or not e.get("value"):
            rep["warnings"].append(f"{f.name}: weak_points[{i}] has no value - dropped")
            continue
        if not e.get("rationale"):
            rep["warnings"].append(f"{f.name}: weak_points[{i}] has no rationale - dropped")
            continue
        _style(rep, f"{f.name} weak_points[{i}]", e["value"])
        _style(rep, f"{f.name} weak_points[{i}] rationale", e["rationale"])
        q, loc = e.get("quote"), e.get("location")
        if q and not quote_in_text(q, text):
            rep["warnings"].append(f"{f.name}: weak_points[{i}] quote not found verbatim in the cached text - quote dropped, point kept: {q!r}")
            q, loc = None, None
        n = evidence(e["value"], "inferred", source=src, checked=today(), location=loc if q else None, quote=q or None,
                     rationale=e["rationale"])
        n["kind"] = e.get("kind") if e.get("kind") in WEAK_POINT_KINDS else "other"
        wp.append(_set_emphasis(n, e, rep, f"{f.name} weak_points[{i}]"))
    x["declared_limitations"], x["weak_points"] = dl, wp
    rep["critiques"] += 1


# --------------------------------------------------------------------------
# step 6: review synthesis
# --------------------------------------------------------------------------

def _kpi_label(rv: Review, k: dict) -> str:
    tgt = rv.tables["benchmarks" if k.get("benchmark_id") else "datasets"].get(k.get("benchmark_id") or k.get("dataset_id") or "") or {}
    m = rv.tables["metrics"].get(k.get("metric_id") or "") or {}
    d = {True: "higher is better", False: "lower is better"}.get(m.get("higher_is_better"), "direction not set")
    v = f" [{k['metric_variant']}]" if k.get("metric_variant") else ""
    return f"{tgt.get('name')} · {m.get('name')}{v} ({d})"


def _kpi_short(rv: Review, k: dict) -> str:
    """The board's label without its direction. Cutting at the first " (" left "MedQA" of "MedQA (USMLE, 4 options)
    accuracy, the paper's own medical model ..." on the video's chart and provenance table (llm-to-domain-slm K009)."""
    return re.sub(r" \((?:higher is better|lower is better|direction not set)\)$", "", _kpi_label(rv, k))


def _availability(p: dict) -> dict:
    """Code and checkpoint status from the enrich cells; anything not found or never enriched reads as unknown, never no."""
    out = {}
    for f in ("code", "checkpoint"):
        e = p.get(f)
        v = e.get("value") if is_evidence(e) and isinstance(e.get("value"), dict) else {}
        out[f] = {"availability": v.get("availability") or "unknown", "kind": v.get("kind"), "url": v.get("url"),
                  "status": e.get("status") if is_evidence(e) else "not-enriched"}
    return out


def synthesis_counts(rv: Review) -> dict:
    """Paper counts computed from the data, for the synthesis writer to copy: writers that counted the member lists
    themselves wrote "7 of the 13 ranked papers" for 12 (retrieval-confidence) and "16 papers" for 15 (MDE)."""
    C, papers = rv.tables["clusters"], rv.tables["papers"]
    rp = rank_papers(rv)
    per, tot = {}, {"with_code": 0, "with_checkpoint": 0, "with_code_and_checkpoint": 0}
    for c in sorted(C.values(), key=lambda c: c["id"]):
        av = [_availability(papers[pid]) for pid in c.get("paper_ids") or [] if pid in papers]
        # a checkpoint counts only when the authors released it: a community model whose card cites the paper (QuIP#:
        # GLM and Qwen quantizations) is not the paper's weights
        ck = [a["checkpoint"]["availability"] == "yes" and a["checkpoint"]["kind"] != "third-party" for a in av]
        n = {"members": len(av), "with_code": sum(a["code"]["availability"] == "yes" for a in av),
             "with_checkpoint": sum(ck),
             "with_code_and_checkpoint": sum(a["code"]["availability"] == "yes" and c for a, c in zip(av, ck))}
        per[c["id"]] = n
        for k in tot:
            tot[k] += n[k]
    ctx = _PresCtx(rv)
    no_result = sum(1 for u in rp["unranked"] if str(u.get("reason") or "").startswith("no result on"))
    return {"families": len(C), "family_members": sum(n["members"] for n in per.values()), "ranked": len(rp["ranked"]),
            "below_cut": len(rp["below_cut"]), "unranked": len(rp["unranked"]),
            "unranked_no_board_result": no_result, "unranked_other": len(rp["unranked"]) - no_result,
            "board_papers": {k["id"]: len(ctx.board(k["id"])) for k in rv.meta.get("kpis") or []},
            "all_families": tot, "per_family": per}


# "13 ranked papers", "16 papers in these four families", "7 of the 13 papers": a count of papers, members or families
_NUM_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
_NUM = r"(\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_COUNT_CLAIM = re.compile(rf"(?<![\w.])(?:{_NUM} of (?:the |these |those )?)?{_NUM}\s+(?:[A-Za-z-]+\s+){{0,2}}?(papers?|members?|famil(?:y|ies))\b", re.I)
# "the four C007 (transformers without scale) members": a count of one family's members ("Four of the five C007 ...")
_FAMILY_COUNT = re.compile(rf"(?<![\w.]){_NUM}\s+(C\d{{3,}})\b[^.;]{{0,60}}?\bmembers?\b", re.I)


def _count_num(s: str | None) -> int | None:
    return None if s is None else int(s) if s.isdigit() else _NUM_WORDS.get(s.lower())


def count_context(rv: Review) -> dict:
    """The computed counts a text's paper counts are checked against (synthesis takeaways, step-8 narration)."""
    def ints(o, out):
        if isinstance(o, bool):
            return out
        if isinstance(o, int):
            out.add(o)
        elif isinstance(o, dict):
            for v in o.values():
                ints(v, out)
        elif isinstance(o, list):
            for v in o:
                ints(v, out)
        return out
    c = synthesis_counts(rv)
    return {"c": c, "anything": ints(c, set()) | ints(_evidence_base(rv), set()),
            "members": {c["family_members"]} | {n["members"] for n in c["per_family"].values()}}


_WHOLE_SET = re.compile(r"\b(?:the|these|those|all|its|only)\s+$", re.I)    # "of the 13 ranked papers", "these four families"


def count_issues(cx: dict, items: list) -> list[str]:
    """Counts of the whole set of ranked papers, family members, families, unranked papers or one family's members, in
    (label, text) items, that do not match the computed count: "7 of the 13 ranked papers" (12), "13 of the 16 papers
    in these four families" (15), "The four C007 members" (5). A count is checked when it is larger than the total or
    when "the", "these", "all" or "only" puts it on the whole set; "2 families" or "the two C001 members without depth"
    is a subset and passes."""
    c, members = cx["c"], cx["members"]
    out = []
    for label, s in items:
        s = str(s or "")
        for m in _COUNT_CLAIM.finditer(s):
            noun, after = m.group(3).lower(), s[m.start(2):m.end() + 25].lower()
            if noun.startswith("famil") and not re.match(r"\s+(?:members?|papers?)\b", s[m.end():], re.I):
                want, what = c["families"], "families"
            elif re.search(r"\branked\b|\bkpi rank|\bhave a rank", after):
                want, what = c["ranked"], "ranked papers"
            elif re.search(r"\bno (?:kpi |board )?result", after):
                want, what = c["unranked_no_board_result"], "papers with no board result"
            elif noun.startswith(("member", "famil")) or re.search(r"\bin (?:these|the|all|its) .{0,12}famil", after):
                want, what = c["family_members"], "family members"
            else:
                continue
            # "In C008, code was found for 2 of the 4 members": the one family the sentence names is the set (MDE G6)
            head = s[:m.start()]
            fams = set(re.findall(r"\b(C\d{3,})\b", head[max(head.rfind(". "), head.rfind("; ")) + 1:]))
            if what == "family members" and noun.startswith("member") and len(fams) == 1 and next(iter(fams)) in c["per_family"]:
                fam = next(iter(fams))
                want, what = c["per_family"][fam]["members"], f"{fam} members"
            n = _count_num(m.group(2))
            # "only the two families that remove weights" names a subset by a clause after the noun (llm-to-domain-slm
            # report, 2026-10-26: three false warnings); a count larger than the whole set is still wrong
            sub = re.match(r"\s+(?:that|which|who|whose|with|without|on|in|from|using|trained|tested|whose)\b", s[m.end():], re.I)
            if n != want and (n > want or (_WHOLE_SET.search(s[:m.start(2)]) and not sub)):
                out.append(f"{label}: {m.group(0)!r} - {m.group(2)} does not match the {want} {what}")
        for m in _FAMILY_COUNT.finditer(s):
            if re.match(r"\s+(?:without|with|that|which|who|whose|on|in|from|using|trained|tested)\b", s[m.end():], re.I):
                continue        # "the two C001 members without depth" counts a subset, not the family
            want, n = (c["per_family"].get(m.group(2).upper()) or {}).get("members"), _count_num(m.group(1))
            if want is not None and n != want and (n > want or _WHOLE_SET.search(s[:m.start(1)])):
                out.append(f"{label}: {m.group(0)[:60]!r} - {m.group(2)} has {want} members")
    return out


def synthesis_count_issues(rv: Review, takeaways: list) -> list[str]:
    """count_issues on the takeaways' text and detail."""
    return count_issues(count_context(rv), [(f"synthesis takeaway {i} {f}", t.get(f)) for i, t in enumerate(takeaways, 1)
                                            for f in ("text", "detail")])


def synthesis_input(rv: Review) -> dict:
    C, X, papers = rv.tables["clusters"], rv.tables["explainers"], rv.tables["papers"]
    if not C:
        raise SystemExit("no clusters: run cluster-input / merge-clusters and the explainers first")
    rp = rank_papers(rv)
    pos = {r["paper_id"]: r for r in rp["ranked"] + rp["below_cut"]}
    clusters = []
    for c in sorted(C.values(), key=lambda c: c["id"]):
        x = next((e for e in X.values() if e.get("cluster_id") == c["id"]), None) or {}
        clusters.append({
            "id": c["id"], "name": c.get("name"), "definition": (c.get("definition") or {}).get("value"),
            "distinguishing_trait": c.get("distinguishing_trait"), "winner": c.get("winner_paper_id"),
            "members": [{"paper_id": pid, "title": papers[pid].get("title"), "notes": _method_notes(papers[pid]),
                         "per_kpi": (pos.get(pid) or {}).get("per_kpi"), "availability": _availability(papers[pid])}
                        for pid in c.get("paper_ids") or [] if pid in papers],
            "explainer": {"headline": x.get("headline"),
                          "facets": {k: (v or {}).get("value") for k, v in (x.get("facets") or {}).items()},
                          "vs_siblings": (x.get("vs_siblings") or {}).get("value")}})
    return {"purpose": rv.meta.get("purpose"),
            "kpis": [{"id": k["id"], "label": _kpi_label(rv, k), "rationale": k.get("rationale")} for k in rv.meta.get("kpis") or []],
            "clusters": clusters, "unranked": rp["unranked"], "counts": synthesis_counts(rv),
            "availability_note": ("each member's `availability` (code, checkpoint) comes from `enrich`: 'yes' is a found "
                                  "official or third-party release; 'unknown' means not found or not checked, never a verified no"),
            "output_path": str(rv.path / "data" / "explainers" / "synthesis.json")}


def merge_synthesis(rv: Review) -> dict:
    f = rv.path / "data" / "explainers" / "synthesis.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the synthesis subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f'{f}: invalid JSON ({e}); expected {{"takeaways": [...]}}; nothing changed')
    items = data.get("takeaways") if isinstance(data, dict) else None
    if not isinstance(items, list) or not any(isinstance(t, dict) and t.get("text") for t in items):
        raise SystemExit(f'{f}: expected {{"takeaways": [{{"text", "rationale", "evidence": [{{"paper_id", "quote"}}]}}]}}; nothing changed')
    rep = {"takeaways": 0, "dropped": 0, "quotes_dropped": 0, "warnings": [], "style": []}
    texts: dict[str, str] = {}
    out = []
    for i, t in enumerate(items):
        if not isinstance(t, dict) or not t.get("text"):
            rep["warnings"].append(f"takeaway {t!r} is not an object with text - skipped")
            rep["dropped"] += 1
            continue
        ev = []
        for e in t.get("evidence") or []:
            pid = e.get("paper_id") if isinstance(e, dict) else None
            if pid not in rv.tables["papers"]:
                rep["warnings"].append(f"takeaway {t['text'][:40]!r}: evidence {e!r} names no known paper - dropped")
                rep["quotes_dropped"] += 1
                continue
            if pid not in texts:
                texts[pid] = rv.read_fulltext(pid)[0] or ""
            if not quote_in_text(e.get("quote"), texts[pid]):
                rep["warnings"].append(f"takeaway {t['text'][:40]!r}: quote for {pid} not in its cached text - dropped: {e.get('quote')!r}")
                rep["quotes_dropped"] += 1
                continue
            ev.append({"paper_id": pid, "quote": e["quote"], "location": e.get("location")})
        if not ev:
            rep["warnings"].append(f"takeaway {t['text'][:60]!r} has no surviving evidence - dropped")
            rep["dropped"] += 1
            continue
        out.append({"text": t["text"], "rationale": t.get("rationale") or "", "evidence": ev,
                    "cluster_ids": [c for c in t.get("cluster_ids") or [] if c in rv.tables["clusters"]]})
        if isinstance(t.get("detail"), str) and t["detail"].strip():
            out[-1]["detail"] = t["detail"].strip()     # the numbers and ranks behind the takeaway, shown folded
        _style(rep, f"synthesis[{i}]", t["text"])
        _style(rep, f"synthesis[{i}] detail", out[-1].get("detail"))
        em = _emphasis(t.get("emphasis"), t["text"], rep, f"takeaway {t['text'][:40]!r}")
        if em is not None:
            out[-1]["emphasis"] = em
    rv.meta["synthesis"] = {"takeaways": out, "source": "data/explainers/synthesis.json", "merged_at": now_iso()}
    rep["takeaways"] = len(out)
    rep["warnings"] += synthesis_count_issues(rv, out)     # send the writer back with them; validate repeats them
    return rep


# --------------------------------------------------------------------------
# step 6: limitations and problems of the current approaches (the synthesis slide's matrix)
# --------------------------------------------------------------------------

LIMIT_STATES = ("affected", "addresses", "not-shown")
LIMIT_BASES = ("author-reported", "inferred")
LIMIT_ROWS, OPEN_PROBLEMS_MAX, LIMIT_TITLE_WORDS = (4, 8), 5, 10


def _missing_member_limits(rv: Review) -> list[str]:
    """Non-winner cluster members whose stated limitations were never merged (no data/explainers/limits/<P-id>.json)."""
    X = {x.get("cluster_id"): x for x in rv.tables["explainers"].values()}
    return [pid for cid, c in sorted(rv.tables["clusters"].items()) if cid in X
            for pid in c.get("paper_ids") or [] if pid != c.get("winner_paper_id")
            and pid not in (X[cid].get("member_limitations") or {})]


def limitations_input(rv: Review) -> dict:
    C, X, papers = rv.tables["clusters"], rv.tables["explainers"], rv.tables["papers"]
    if not X:
        raise SystemExit("no explainers: write the slides and winner critiques first (explainer-input -> subagents -> merge-explainers)")
    rp = rank_papers(rv)
    pos = {r["paper_id"]: r for r in rp["ranked"] + rp["below_cut"]}
    clusters = []
    for c in sorted(C.values(), key=lambda c: c["id"]):
        x = next((e for e in X.values() if e.get("cluster_id") == c["id"]), None) or {}
        w, ml = c.get("winner_paper_id"), x.get("member_limitations") or {}
        members = []
        for pid in [w] + [s for s in c.get("paper_ids") or [] if s != w]:
            if pid not in papers:
                continue
            dl = (x.get("declared_limitations") if pid == w else ml.get(pid)) or []
            members.append({"paper_id": pid, "title": papers[pid].get("title"), "role": "winner" if pid == w else "member",
                            "notes": _method_notes(papers[pid]), "per_kpi": (pos.get(pid) or {}).get("per_kpi"),
                            "availability": _availability(papers[pid]),
                            "declared_limitations": [{k: e.get(k) for k in ("value", "quote", "location")}
                                                     for e in dl if e.get("value") is not None]
                                                    or (dl[0].get("status") if dl else "not extracted")})
        clusters.append({"id": c["id"], "name": c.get("name"), "distinguishing_trait": c.get("distinguishing_trait"),
                         "winner": w, "members": members,
                         "winner_weak_points": [{k: e.get(k) for k in ("value", "kind", "rationale", "quote", "location")}
                                                for e in x.get("weak_points") or []]})
    return {"purpose": rv.meta.get("purpose"), "scope": rv.meta.get("scope"),
            "kpis": [{"id": k["id"], "label": _kpi_label(rv, k)} for k in rv.meta.get("kpis") or []],
            "kinds": WEAK_POINT_KINDS, "clusters": clusters, "missing_member_limits": _missing_member_limits(rv),
            "availability_note": ("each member's `availability` (code, checkpoint) comes from `enrich`: 'yes' is a found "
                                  "official or third-party release; 'unknown' means not found or not checked, never a verified no"),
            "output_path": str(rv.path / "data" / "explainers" / "limitations.json")}


def _limit_cell(rv: Review, cid: str, cell, texts: dict, rep: dict, where: str) -> dict:
    """One matrix cell, checked: `affected` or `addresses` names a member of that family; an author-reported cell (and
    every `addresses` cell) quotes that paper's cached text; an inferred `affected` cell gives a rationale and may quote.
    A cell that fails is `not-shown`, never dropped silently."""
    blank = {"state": "not-shown"}
    if cell is None:
        return blank
    if not isinstance(cell, dict) or cell.get("state") not in LIMIT_STATES:
        rep["warnings"].append(f"{where}: {cell!r} is not a cell with a state in {LIMIT_STATES} - not-shown")
        return blank
    if cell["state"] == "not-shown":
        return dict(blank, rationale=cell["rationale"]) if cell.get("rationale") else blank
    pid = cell.get("paper_id")
    if pid not in (rv.tables["clusters"][cid].get("paper_ids") or []):
        rep["warnings"].append(f"{where}: paper {pid!r} is not a member of {cid} - not-shown")
        rep["downgraded"] += 1
        return blank
    basis = "author-reported" if cell["state"] == "addresses" else cell.get("basis")
    if basis not in LIMIT_BASES:
        basis = "author-reported" if cell.get("quote") and not cell.get("rationale") else "inferred"
    if pid not in texts:
        texts[pid] = rv.read_fulltext(pid)[0] or ""
    q = cell.get("quote") if isinstance(cell.get("quote"), str) and cell["quote"].strip() else None
    if q and not quote_in_text(q, texts[pid]):
        if basis == "author-reported":
            rep["warnings"].append(f"{where}: quote not in {pid}'s cached text - not-shown: {q!r}")
            rep["downgraded"] += 1
            return blank
        rep["warnings"].append(f"{where}: quote not in {pid}'s cached text - quote dropped, cell kept: {q!r}")
        q = None
    if basis == "author-reported" and not q:
        rep["warnings"].append(f"{where}: {cell['state']} on the authors' words needs a quote from {pid} - not-shown")
        rep["downgraded"] += 1
        return blank
    if basis == "inferred" and not cell.get("rationale"):
        rep["warnings"].append(f"{where}: the reviewer's reading needs a rationale - not-shown")
        rep["downgraded"] += 1
        return blank
    out = {"state": cell["state"], "basis": basis, "paper_id": pid}
    if q:
        out["quote"] = q
        if cell.get("location"):
            out["location"] = cell["location"]
    if cell.get("rationale"):
        out["rationale"] = cell["rationale"]
        _style(rep, f"{where} rationale", cell["rationale"])
    return out


def merge_limitations(rv: Review) -> dict:
    f = rv.path / "data" / "explainers" / "limitations.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the review-limitations subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f'{f}: invalid JSON ({e}); expected {{"problems": [...], "open_problems": [...]}}; nothing changed')
    items = data.get("problems") if isinstance(data, dict) else None
    if not isinstance(items, list) or not any(isinstance(t, dict) and t.get("text") for t in items):
        raise SystemExit(f'{f}: expected {{"problems": [{{"title", "kind", "text", "cells": {{"C001": {{"state", ...}}}}}}], '
                         f'"open_problems": [...]}}; nothing changed')
    C = rv.tables["clusters"]
    need = min(2, len(C))
    rep = {"problems": 0, "dropped": 0, "downgraded": 0, "open_problems": 0, "warnings": [], "style": []}
    texts: dict[str, str] = {}
    out, ids = [], set()
    given = {str(t.get("id")) for t in items if isinstance(t, dict) and t.get("id")}
    for i, t in enumerate(items):
        if not isinstance(t, dict) or not t.get("text") or not t.get("title"):
            rep["warnings"].append(f"problems[{i}]: needs a title and a text - dropped")
            rep["dropped"] += 1
            continue
        lid = str(t.get("id") or "").strip()
        if not lid or lid in ids:
            lid = next(f"L{n}" for n in range(1, len(items) + len(given) + 2) if f"L{n}" not in ids | given)
        cells_in = t.get("cells") if isinstance(t.get("cells"), dict) else {}
        for cid in cells_in:
            if cid not in C:
                rep["warnings"].append(f"{lid}: cell for unknown family {cid} - ignored")
        cells = {cid: _limit_cell(rv, cid, cells_in.get(cid), texts, rep, f"{lid}.{cid}") for cid in sorted(C)}
        hit = sum(1 for c in cells.values() if c["state"] == "affected")
        if hit < need:
            rep["warnings"].append(f"{lid} {t['title']!r}: {hit} affected famil{'y' if hit == 1 else 'ies'} after the checks "
                                   f"(needs {need}: a problem of one family belongs on its slide) - dropped")
            rep["dropped"] += 1
            continue
        ids.add(lid)
        row = {"id": lid, "title": " ".join(str(t["title"]).split()),
               "kind": t.get("kind") if t.get("kind") in WEAK_POINT_KINDS else "other", "text": t["text"], "cells": cells}
        if len(row["title"].split()) > LIMIT_TITLE_WORDS:
            rep["style"].append(f"{lid} title: {len(row['title'].split())} words (max {LIMIT_TITLE_WORDS})")
        if isinstance(t.get("detail"), str) and t["detail"].strip():
            row["detail"] = t["detail"].strip()
        _style(rep, f"{lid} text", t["text"])
        _style(rep, f"{lid} detail", row.get("detail"))
        em = _emphasis(t.get("emphasis"), t["text"], rep, f"{lid}")
        if em is not None:
            row["emphasis"] = em
        out.append(row)
    lo, hi = LIMIT_ROWS
    if not lo <= len(out) <= hi:
        rep["warnings"].append(f"{len(out)} problem rows survived (want {lo}-{hi})")
    opens = []
    for i, o in enumerate(data.get("open_problems") or []):
        if not isinstance(o, dict) or not o.get("text") or not o.get("title"):
            rep["warnings"].append(f"open_problems[{i}]: needs a title and a text - dropped")
            continue
        if len(opens) == OPEN_PROBLEMS_MAX:
            rep["warnings"].append(f"open_problems: more than {OPEN_PROBLEMS_MAX} - kept the first {OPEN_PROBLEMS_MAX}")
            break
        row = {"title": " ".join(str(o["title"]).split()), "text": o["text"], "basis": "outside-knowledge",
               "related": [r for r in o.get("related") or [] if r in ids]}
        if re.search(r"\d", re.sub(r"\b[CP]\d{3,}\b", "", f"{o['title']} {o['text']}")):
            rep["warnings"].append(f"open_problems[{i}]: names a number; an outside-knowledge item cannot be checked, so it states none")
        _style(rep, f"open_problems[{i}]", o["text"])
        em = _emphasis(o.get("emphasis"), o["text"], rep, f"open_problems[{i}]")
        if em is not None:
            row["emphasis"] = em
        opens.append(row)
    X = [x for x in rv.tables["explainers"].values() if x.get("cluster_id") in C]
    rv.meta["limitations"] = {"problems": out, "open_problems": opens, "source": "data/explainers/limitations.json",
                              "basis": {"winners": len(X), "members": sum(len(x.get("member_limitations") or {}) for x in X),
                                        "missing_members": _missing_member_limits(rv)},
                              "merged_at": now_iso()}
    rep["problems"], rep["open_problems"] = len(out), len(opens)
    if rv.meta["limitations"]["basis"]["missing_members"]:
        rep["warnings"].append("members with no merged stated limitations: " + ", ".join(rv.meta["limitations"]["basis"]["missing_members"])
                               + " (member-limitations subagent, then merge-explainers, then rerun this)")
    return rep


# --------------------------------------------------------------------------
# step 6: emphasis (the words the hub highlights in the long texts)
# --------------------------------------------------------------------------

def _emphasis_slots(rv: Review) -> list[tuple]:
    """Every long step-6 text in its source file, as (text id, file, file data, item): the four slide fields, each
    declared limitation and weak point, each takeaway, and the step-5 method notes of each slide's paper that its
    "In this review" strip shows (id "P001.loss", file data/details/P001.json; only a note whose merged text is the
    file's text). The text is the item's `value` (`text` for a takeaway, a limitations problem or an open problem).
    Headlines, facets, glossary entries and problem titles are short and get no emphasis."""
    xdir = rv.path / "data" / "explainers"
    out = []
    for f in (sorted(xdir.glob("C*.json")) + [xdir / "synthesis.json"]) if xdir.exists() else []:
        m = re.fullmatch(r"(C\d{3,})(\.critique)?|synthesis", f.stem)
        if not m or not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if f.stem == "synthesis":
            out += [(f"synthesis[{i}]", f, data, t) for i, t in enumerate(data.get("takeaways") or [])
                    if isinstance(t, dict) and isinstance(t.get("text"), str)]
        elif m.group(2):
            out += [(f"{m.group(1)}.{k}[{i}]", f, data, e) for k in ("declared_limitations", "weak_points")
                    for i, e in enumerate(data.get(k) or []) if isinstance(e, dict) and isinstance(e.get("value"), str)]
        else:
            for k in EXPLAINER_FIELDS:
                if isinstance(data.get(k), str):             # a bare-string vs_siblings: merge-explainers reads both shapes
                    data[k] = {"value": data[k]}
                if isinstance(data.get(k), dict) and isinstance(data[k].get("value"), str):
                    out.append((f"{m.group(1)}.{k}", f, data, data[k]))
    lf = xdir / "limitations.json"
    try:
        lim = json.loads(lf.read_text())
    except (OSError, json.JSONDecodeError):
        lim = None
    if isinstance(lim, dict):
        out += [(f"limitations.{t.get('id') or i}", lf, lim, t) for i, t in enumerate(lim.get("problems") or [])
                if isinstance(t, dict) and isinstance(t.get("text"), str)]
        out += [(f"limitations.open[{i}]", lf, lim, o) for i, o in enumerate(lim.get("open_problems") or [])
                if isinstance(o, dict) and isinstance(o.get("text"), str)]
    for pid in sorted({x.get("paper_id") for x in rv.tables["explainers"].values()} - {None}):
        f, p = rv.path / "data" / "details" / f"{pid}.json", rv.tables["papers"].get(pid) or {}
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out += [(f"{pid}.{k}", f, data, data[k]) for k in DETAIL_FIELDS
                    if isinstance(data.get(k), dict) and isinstance(data[k].get("value"), str)
                    and is_evidence(p.get(k)) and p[k].get("value") == data[k]["value"]]
    return out


def _slot_text(item: dict) -> str:
    return item["text"] if isinstance(item.get("text"), str) else item.get("value")


def emphasis_input(rv: Review) -> dict:
    """Packet for the emphasis subagent: every long step-6 text whose writer gave no `emphasis` (reviews made before
    v1.22.0, or a writer that left it out), and the method notes of the slides' papers (no writer marks those)."""
    return {"purpose": rv.meta.get("purpose"), "max_phrases": EMPHASIS_MAX, "max_words": EMPHASIS_WORDS,
            "texts": [{"id": sid, "text": _slot_text(item)} for sid, _, _, item in _emphasis_slots(rv) if "emphasis" not in item],
            "output_path": str(rv.path / "data" / "explainers" / "emphasis.json")}


def merge_emphasis(rv: Review) -> dict:
    """Write data/explainers/emphasis.json ({text id: [phrases]}) into the step-6 source files (a method note's into
    data/details/<P-id>.json), then re-merge them, so a later merge-explainers, merge-synthesis or merge-details keeps
    the highlights. Only `emphasis` keys change in those files; a text whose phrases all fail keeps none and stays in
    the next emphasis-input."""
    f = rv.path / "data" / "explainers" / "emphasis.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the emphasis subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{f}: invalid JSON ({e}); nothing changed")
    if not isinstance(data, dict):
        raise SystemExit(f'{f}: expected {{"<text id>": ["phrase", ...]}}; nothing changed')
    rep = {"set": 0, "unknown_ids": [], "warnings": []}
    slots = {sid: (path, fd, item) for sid, path, fd, item in _emphasis_slots(rv)}
    changed = {}
    for sid, phrases in data.items():
        if sid not in slots:
            rep["unknown_ids"].append(sid)
            continue
        path, fd, item = slots[sid]
        em = _emphasis(phrases if isinstance(phrases, list) else [phrases], _slot_text(item), rep, sid)
        if phrases and not em:
            continue
        item["emphasis"] = em
        changed[path] = fd
        rep["set"] += 1
        if path.parent.name == "details":   # a method note: the papers table holds its merged copy
            pid, fld = sid.split(".", 1)
            rv.tables["papers"][pid][fld]["emphasis"] = em
    for path, fd in changed.items():
        path.write_text(json.dumps(fd, indent=1, ensure_ascii=False) + "\n")
        if path.parent.name == "details" and path.stem in (rv.meta.get("details_merged") or {}):
            # the papers table got the same phrases, so the file still matches the merge: `status` must not call it stale
            rv.meta["details_merged"][path.stem] = hashlib.sha1(path.read_bytes()).hexdigest()
    if any(p.parent.name == "explainers" and p.stem not in ("synthesis", "limitations") for p in changed):
        mx = merge_explainers(rv)
        rep["explainers"] = {k: mx[k] for k in ("merged", "critiques", "skipped")}
    if any(p.stem == "synthesis" for p in changed) and rv.meta.get("synthesis"):
        rep["synthesis"] = {"takeaways": merge_synthesis(rv)["takeaways"]}
    if any(p.stem == "limitations" for p in changed) and rv.meta.get("limitations"):
        rep["limitations"] = {"problems": merge_limitations(rv)["problems"]}
    return rep


# --------------------------------------------------------------------------
# step 8 (opt-in): presentation - the chapter plan and the narration script a video is built from
# --------------------------------------------------------------------------

# The ten sections of an applied-research literature review (the user's spec, 2026-10-26: "make sure the video has this
# information", 30 minutes by default (20 again since 2026-10-28), then "make sure that this information is also available in the hub itself"):
# (name, the question it answers). The hub's Report tab shows all ten (step 7, report-input -> merge-report); in a video
# each chapter carries one or more and every section is on some chapter. Plans merged before v1.34.0 (schema 1) carry
# the first guidelines' eight chapter functions instead; _card_sections maps them onto the sections.
PRES_SECTIONS = {
    1: ("Executive summary", "What are the main findings, and what do they mean for the review's purpose?"),
    2: ("Problem definition, scope and requirements", "What problem is solved, and under which constraints?"),
    3: ("Search strategy and review method", "How was the literature found and selected, and what does the review cover?"),
    4: ("Background, task definitions and taxonomy", "How is the field organized, and which distinctions matter?"),
    5: ("Datasets, benchmarks and evaluation metrics", "How are the approaches evaluated, and how relevant are those evaluations?"),
    6: ("Method families and technical analysis", "How do the approaches work, and which design choices matter?"),
    7: ("Comparative results and evidence quality", "What works, under which conditions, and how strongly do the results support it?"),
    8: ("Practical feasibility and reproducibility", "What can a team realistically reproduce, adapt and deploy?"),
    9: ("Synthesis, limitations and research gaps", "What was learned across the papers, and what remains unresolved?"),
    10: ("Recommendations and experimental roadmap", "What should be built or tested next, and why?")}
PRES_PLAN_SCHEMA = 2
PRES_FUNCTIONS_V1 = {1: "question, stakes and a qualified headline answer", 2: "scope, evidence selection and coverage limits",
                     3: "map of the field", 4: "definitions, assumptions and criteria for comparing the evidence",
                     5: "a central theme or approach: explanation, evidence and qualifications",
                     6: "synthesis across the evidence: convergence, disagreement, conditional conclusions",
                     7: "confidence, gaps and boundaries of the findings", 8: "implications, next steps and final takeaways"}
PRES_V1_SECTIONS = {1: (1,), 2: (2, 3), 3: (4,), 4: (4, 5), 5: (6, 7), 6: (9,), 7: (7, 9), 8: (10,)}
# seconds per section at 5, 10, 20 and 30 minutes; other lengths scale the nearest one. The first 30-minute video
# (llm-to-domain-slm, 2026-10-26: 90, 90, 90, 150, 180, 540, ...) reached its first method at 10:16; an outside review
# asked for a mechanism within minutes and the search log in under a minute. Since 2026-10-28 the video opens with a
# top-down verdict (the user: "start with top down summary (which is better on what principles) before getting into
# details for each family"), so section 1 holds two data scenes; sections 7 and 9 no longer gather the caveats the
# family chapters said (the user: "do not repeat sentences and even subjects/claims"). At 20 minutes the methods start
# at 5:00.
PRES_ALLOC = {5: (30, 15, 15, 25, 30, 80, 35, 20, 25, 25), 10: (50, 30, 30, 40, 60, 175, 65, 40, 45, 65),
              20: (100, 40, 30, 70, 60, 470, 130, 80, 90, 130), 30: (120, 60, 45, 120, 75, 690, 210, 120, 135, 225)}
# 152 words a minute of the SPOKEN text (lexicon applied), plus the 0.3 s gap between sentences (AUDIO_SENT_GAP_S) and
# each scene's pause. Fitted on the 36 measured chapters of three 20-minute videos (2026-10-23; the 145 of 2026-10-21
# over-estimated them by 4.0%), then checked on 6 held-out chapters, 4 of them in short sentences: 1.2% mean error,
# bias +0.3%. A fixed time per sentence (0.42 s + 165 wpm) fitted the 36 as well but missed the held-out ones by +3.1%.
PRES_WPM, PRES_MIN_CHAPTER_S, PRES_PAUSE_S, PRES_PREDICTION_PAUSE_S = 152, 15, 0.5, 1.5
PRES_SENT_GAP_S = 0.3
# a narration sentence carries too much for a listener when it has over 18 words, or a load of 3+: clause marks, plus
# names after the first, plus result numbers after the first (13-23% of the first three videos' sentences)
PRES_DENSE_WORDS, PRES_DENSE_LOAD = 18, 3
PRES_CLAUSE = re.compile(r",|;|:|\u2014|\u2013| - |\(")
_PRES_NAME_TOK = r"(?:[A-Za-z]*[a-z][A-Z][\w'-]*|[A-Za-z]+\d[\w'-]*|[A-Z]{2,}[\w'-]*)"
PRES_NAME = re.compile(rf"\b{_PRES_NAME_TOK}(?:\s+(?:{_PRES_NAME_TOK}|\d+\b(?!\.\d)))*")      # "TREC DL 2019" is one name
PRES_BUDGET = 0.85      # share of PRES_WPM a chapter's caption words fill (gaps, pauses, respellings): ~2.15 words/s,
                        # what the held-out short-sentence chapters measured (2.15-2.24 a second of audio)
PRES_MINUTES, PRES_DEFAULT_MINUTES = (3, 60), 20      # the user's default length (2026-10-28; 30 on 2026-10-26-27)
PRES_DEFAULT_AUDIENCE = "researchers in a neighbouring field who know machine learning but not this literature"
PRES_CLAIMS = ("reported", "synthesis", "action", "illustrative")
PRES_TRANSITIONS = ("cut", "fade", "none")
PRES_ENGAGEMENT = ("prediction", "reveal", "callback")
# how a chapter opens; the first 30-minute video opened almost every chapter with a question and ran question ->
# explanation -> catch -> hospital team -> takeaway (an outside review: the transitions became predictable)
PRES_OPENINGS = ("question", "result", "failure", "constraint", "prediction", "example", "contrast")
# narration habits a listener notices when they repeat (18 x "hospital team", 12 x "invented", 40 x "board", 14 x
# "catch" in the first 30-minute video): notes, like density
PRES_WORDING = [
    (re.compile(r"\bcatch(es)?\b", re.I), "say the limitation itself, not 'catch'"),
    (re.compile(r"\bboards?\b", re.I), "'board' blurs the review's own comparison and a public leaderboard: say 'comparison', 'test' or the test set's name"),
    (re.compile(r"\bbest papers?\b", re.I), "say 'representative paper' (the best-ranked member with a venue), not 'best paper'"),
    (re.compile(r"\b(more|other|two|three|\d+)\b[^.]{0,30}\bon (the )?screen\b", re.I), "say the caveat that changes the claim; do not point at others on screen"),
    (re.compile(r"\b(just|only|merely|a mere|a whopping|a full)\s+(?=\d)", re.I), "state the number; a judgement of its size needs the decision criterion"),
]
PRES_INVENTED = re.compile(r"\b(invented|imaginary|hypothetical|made[- ]up|fictional)\b", re.I)
PRES_EXAMPLE_MAX_CHAPTERS = 5
# Say each thing once (the user, 2026-10-28: "do not repeat sentences and even subjects/claims"). The first 30-minute
# video stated its shared caveat "baselines trained on other data" (L1) in 6 chapters and held 26 sentence pairs that
# say the same thing again: the prompts told writers to say each key message twice and the evidence chapters to gather
# the caveats. Now each card names the `claims` it states (one chapter each: the plan merge refuses a claim on two
# cards), and the script checks list a sentence another chapter, or the same one, already said: 4+ shared content words
# that are 60%+ of the shorter sentence's (on video v3 these 26 pairs were all restated claims; 3 shared words also
# matched formulas such as "reports the highest score").
PRES_CLAIM_ID = re.compile(r"(?:TK|CN|VD|OP|[LUGX])\d+")
PRES_REPEAT_SHARED, PRES_REPEAT_SHARE = 4, 0.6
# At the seam between two chapters the test takes 3 shared words: a chapter ends by asking the next one's question and
# the next asks it again ("Which ones would settle the choices between routes?" / "Which experiments would settle the
# route choices?", CH14 -> CH15 of the first 20-minute replay, 2026-10-28)
PRES_SEAM_SHARED, PRES_SEAM_SENTENCES = 3, 2
_PRES_STOP = frozenset(
    "a an the and or but so of to in on for with by from at as is are was were be been being it its it's this that these "
    "those which who what how why when where than then there here do does did not no can could would should will may "
    "might more most less only also just very their they them our we you your one two three four five each every all any "
    "some into over under about after before between same other another such has have had don't doesn't isn't aren't "
    "can't won't that's here's let's what's there's".split())
# record ids a viewer must never read: families, papers, KPIs, result rows, takeaways, report items, decisions
_VIEWER_IDS = r"(?:C\d{3,}|P\d{3,}|K\d{3,}|R\d{4,}|TK\d+|CN\d+|VD\d+|DEC\d{3,}|OP\d+|[LUGX]\d{1,2})"
PRES_SMALL_INT = 12                     # "three families", "ranks 2 of 4": small counts pass the number check
PRES_TIMING_WARN, PRES_TIMING_ERR = 0.08, 0.20
PRES_BAD_TITLE = re.compile(r"^\s*(paper\s*\d+|more methods|additional results|other (papers|methods))\b", re.I)
PRES_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s*%)?(?![\w])")
PRES_NUM_UNIT = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?()(?=[KMBTx]\b)")      # "3.52B", "7B", "1.5x" in records
# a number written as a word; a scale word after digits ("25.5 billion tokens") is fine: the check reads the digits
PRES_NUM_WORDS = re.compile(r"\b(thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
                            r"sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b(?<!\d\shundred)(?<!\d\sthousand)"
                            r"(?<!\d\smillion)(?<!\d\sbillion)", re.I)
COMPARE_FACETS = ["problem", "key_idea", "how_it_works"] + FACETS
# What the papers are tested on: the plan gives each its own scene, drawn from the review's tables, before any result
# is compared (the user, 2026-10-26: "add to the video sections that summarize relevant tasks, main benchmarks, main
# KPI", "kpi = metrics"). The first four videos had none: their tasks, test sets and metrics were never on screen.
PRES_SUMMARY_KINDS = ("tasks", "benchmarks", "metrics")
PRES_SUMMARY_ROWS, PRES_SUMMARY_MIN_S = 8, 20
# The data scenes each section must show, each in a scene of its own, drawn from the review's tables or from the review
# report (executive summary, requirements, unmeasured, conditions, gaps, roadmap: merge-report checked them): a chapter
# lists them in `summaries`, the plan merge refuses a plan that shows none of a kind, the script merge a chapter without
# its scene. "dimensions" (data, representation, architecture, losses, training of 1-3 papers) is per family: every
# section-6 chapter with a family lists it. The first five videos showed none of these but the three summaries.
# "standing" is per family too, and opens its chapter: where the family's members rank on each KPI comparison, beside
# the leaders (the user, 2026-11-01: "it is very hard to get the context between the lines of work. please show for
# each line of work the place in the leader boards before its description"). The merge adds it to a family chapter.
PRES_SECTION_SCENES = {1: ("executive-summary", "verdict"), 2: ("requirements", "scope"), 3: ("search",), 4: ("tasks",),
                       5: ("benchmarks", "metrics", "unmeasured"), 6: ("standing", "dimensions"), 7: ("provenance", "conditions"),
                       8: ("feasibility",), 9: ("gaps",), 10: ("roadmap",)}
PRES_DATA_KINDS = tuple(k for ks in PRES_SECTION_SCENES.values() for k in ks)
# The executive summary's parts. Since v1.35.0 the strongest findings and the most promising approaches are the
# `verdict` table (report_verdict), so a summary tells the problem, the main uncertainty and the next step around it;
# a report merged before keeps its six parts (PRES_EXEC_LABELS draws whichever it has).
PRES_EXEC_KEYS = {"problem": "Problem", "uncertainty": "Main uncertainty", "next_step": "Recommended next step"}
PRES_EXEC_LABELS = {"problem": "Problem", "families": "Method families", "findings": "Strongest findings",
                    "promising": "Most promising for the purpose", "uncertainty": "Main uncertainty", "next_step": "Recommended next step"}
PRES_REQ_KEYS = {"application_goal": "Application goal", "formal_task": "Research task", "inputs_outputs": "Inputs and outputs",
                 "operating_conditions": "Operating conditions", "constraints": "Constraints", "research_questions": "Research questions"}
PRES_DIMENSIONS = {"data": "Data", "representation": "Representation", "topology": "Architecture",
                   "loss": "Losses and objectives", "training_scheme": "Training scheme"}      # the spec's five, in its order
# kind: (refs it takes, what its reveal ids are). Phase 4 draws every kind from review data, so no number on screen is
# a writer's: free-form scene HTML is not a kind.
VISUAL_KINDS = {
    "title": ("none", "none"), "chapter": ("none", "none"),
    "claim": ("record ids, optional", "line indexes of on_screen"),
    "scope": ("none", "in, out, protocol"), "evidence-flow": ("none", "flow stage names"),
    "taxonomy": ("cluster ids (empty = all)", "cluster ids"), "diagram": ("one cluster id", "the cluster's diagram node ids"),
    "figure": ("one paper id with an extracted figure", "none"), "results-table": ("one cluster id", "row labels"),
    "kpi-chart": ("one KPI id", "paper ids on the board"), "family-table": ("none", "cluster ids"),
    "matrix": ("none", "limitation problem ids"), "compare": ("two cluster ids", "facet names: " + ", ".join(COMPARE_FACETS)),
    "takeaways": ("none", "TK ids"), "open-problems": ("none", "OP ids"),
    "example": ("none; the scene is illustrative", "line indexes of on_screen"),
    "tasks": ("none", "task ids (T), most papers first"), "benchmarks": ("none", "test-set ids (D, or B for a benchmark with no single dataset), most papers first"),
    "metrics": ("none", "metric ids (M), most papers first"),
    "executive-summary": ("none", "parts of the report's executive summary: " + ", ".join(PRES_EXEC_KEYS)),
    "verdict": ("none", "the report's verdict rows (VD1, VD2, ...): which approach is better for which goal, and why"),
    "requirements": ("none", "parts of the report's requirements: " + ", ".join(PRES_REQ_KEYS)),
    "search": ("none", "sources, terms, walks, criteria, gaps"),
    "dimensions": ("1-3 paper ids with method notes", "dimension names: " + ", ".join(PRES_DIMENSIONS)),
    "standing": ("one cluster id", "KPI ids: the comparisons, each with the family's ranks beside the leaders"),
    "unmeasured": ("none", "the report's unmeasured ids (U1, U2, ...)"),
    "provenance": ("none", "KPI ids"), "weak-points": ("none", "cluster ids"),
    "conditions": ("none", "the report's condition ids (CN1, CN2, ...)"), "feasibility": ("none", "paper ids"),
    "gaps": ("none", "the report's gap ids (G1, G2, ...)"),
    "roadmap": ("none", "baseline, shortlist and the report's experiment ids (X1, X2, ...)")}
_PRES_NO_REFS = ("title", "chapter", "scope", "evidence-flow", "family-table", "matrix", "takeaways", "open-problems",
                 "example") + PRES_SUMMARY_KINDS + ("executive-summary", "verdict", "requirements", "search", "unmeasured", "provenance",
                                                    "weak-points", "conditions", "feasibility", "gaps", "roadmap")
_PRES_TABLE = {"P": "papers", "C": "clusters", "R": "results", "T": "tasks", "D": "datasets", "B": "benchmarks", "M": "metrics"}


def _pres_dir(rv: Review) -> Path:
    return rv.path / "data" / "presentation"


def load_presentation(rv: Review) -> dict:
    """The merged chapter plan, scripts and lexicon (data/presentation/merged.json; only the merges write it)."""
    f = _pres_dir(rv) / "merged.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_presentation(rv: Review, d: dict) -> None:
    _pres_dir(rv).mkdir(parents=True, exist_ok=True)
    (_pres_dir(rv) / "merged.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))


def presentation_set(rv: Review, minutes: int = PRES_DEFAULT_MINUTES, audience: str | None = None, by: str = "user", off: bool = False) -> dict:
    """Record the person's answer to the opt-in question: the presentation costs tokens, so step 8 runs only when wanted."""
    cur = dict(rv.meta.get("presentation") or {})
    if off:
        cur.update(wanted=False, by=by, at=now_iso())
    else:
        if not PRES_MINUTES[0] <= int(minutes) <= PRES_MINUTES[1]:
            raise SystemExit(f"--minutes must be {PRES_MINUTES[0]}-{PRES_MINUTES[1]}; got {minutes}")
        cur.update(wanted=True, minutes=int(minutes), audience=audience or cur.get("audience") or PRES_DEFAULT_AUDIENCE,
                   by=by, at=now_iso())
    rv.meta["presentation"] = cur
    return cur


def pres_allocation(minutes: int) -> list[dict]:
    """Seconds per review section for this length: the nearest of 5/10/20/30 minutes, scaled, summing exactly."""
    total = int(minutes) * 60
    base = min(PRES_ALLOC, key=lambda k: (abs(k - minutes), k))
    secs = [round(s * total / (base * 60)) for s in PRES_ALLOC[base]]
    secs[secs.index(max(secs))] += total - sum(secs)
    return [{"section": n, "name": PRES_SECTIONS[n][0], "seconds": s} for n, s in zip(PRES_SECTIONS, secs)]


def _card_sections(card: dict) -> list[int]:
    """A chapter's review sections; a plan merged before v1.34.0 (schema 1) gives functions 1-8, mapped onto them."""
    if card.get("sections") is not None:
        return [s for s in card.get("sections") or [] if s in PRES_SECTIONS]
    return sorted({s for f in card.get("functions") or [] for s in PRES_V1_SECTIONS.get(f, ())})


def _pres_scene_min_s(minutes) -> int:
    """The least time a chapter gives each of its data scenes: 20 s, or 10 s in a video under 10 minutes."""
    return PRES_SUMMARY_MIN_S if int(minutes or PRES_DEFAULT_MINUTES) >= 10 else PRES_SUMMARY_MIN_S // 2


def _vals(t):
    return t.values() if isinstance(t, dict) else t


def _excluded_group(reason) -> str:
    r = str(reason or "").lower()
    if "over cap" in r or "seminal cap" in r:
        return "cap"
    if "year window" in r:
        return "window"
    if "full text" in r or "text access" in r or "abstract-only" in r:
        return "text"
    return "topic"


def _evidence_base(rv: Review) -> dict:
    """How the papers were found and narrowed, computed from the data: the video's scope chapter may not describe the
    search from the purpose text."""
    P, runs, terms, caps = rv.tables["papers"], rv.tables["search_runs"], rv.tables["search_terms"], rv.meta.get("caps") or {}
    exc: dict[str, int] = {}
    for p in P.values():
        if p.get("status") == "excluded":
            g = _excluded_group(p.get("status_reason"))
            exc[g] = exc.get(g, 0) + 1
    inc = [p for p in P.values() if p.get("status") == "included"]
    kinds: dict[str, int] = {}
    for r in _vals(runs):
        kinds[r.get("kind") or "other"] = kinds.get(r.get("kind") or "other", 0) + 1
    hops = [int(h) for h in ((r.get("filters") or {}).get("hop") for r in _vals(runs)) if isinstance(h, int)]
    tstat: dict[str, int] = {}
    for t in _vals(terms):
        tstat[t.get("status") or "pending"] = tstat.get(t.get("status") or "pending", 0) + 1
    front = snowball_frontier(rv)[0] if inc else []
    return {"flow": [{"stage": "found", "n": len(P), "label": "papers found by keyword searches and citation walks"},
                     {"stage": "window", "n": exc.get("window", 0), "label": "outside the year window"},
                     {"stage": "topic", "n": exc.get("topic", 0), "label": "screened out as off-topic"},
                     {"stage": "text", "n": exc.get("text", 0), "label": "no readable full text"},
                     {"stage": "cap", "n": exc.get("cap", 0), "label": "over the paper cap"},
                     {"stage": "included", "n": len(inc), "label": "included and read"}],
            "candidates_left": sum(1 for p in P.values() if p.get("status") == "candidate"),
            "screened": sum(1 for p in P.values() if p.get("screening") or p.get("manual_at")),
            "search_terms": tstat, "runs_by_kind": kinds, "max_snowball_hop": max(hops) if hops else None,
            "snowball_converged": not front, "coverage_check_run": (rv.path / "data" / "coverage.json").exists(),
            "surveys_used": [{"paper_id": p["id"], "title": p.get("title")} for p in sorted(P.values(), key=lambda p: p["id"])
                             if p.get("taxonomy_source")],
            "year_window": caps.get("years"), "paper_cap": caps.get("max_papers"),
            "note": ("The search was selective (keyword searches on arXiv, citation walks, snowballing and a coverage check "
                     "against Semantic Scholar), not a systematic review: say so, and never imply an exhaustive search.")}


def _pres_ref_ok(rv: Review, ref) -> bool:
    if not isinstance(ref, str):
        return False
    m = re.fullmatch(r"([PCRTDBM])\d{3,}", ref)
    if m:
        return ref in rv.tables[_PRES_TABLE[m.group(1)]]
    if re.fullmatch(r"K\d{3,}", ref):
        return any(k.get("id") == ref for k in rv.meta.get("kpis") or [])
    if re.fullmatch(r"DEC\d{3,}", ref):
        return any(d.get("id") == ref for d in rv.meta.get("decisions") or [])
    if re.fullmatch(r"L\d+", ref):
        return any(p.get("id") == ref for p in (rv.meta.get("limitations") or {}).get("problems") or [])
    if re.fullmatch(r"A\d+", ref):
        return any(t.get("id") == ref for t in (rv.meta.get("adjacent") or {}).get("topics") or [])
    m = re.fullmatch(r"(TK|OP)(\d+)", ref)
    if m:
        lst = ((rv.meta.get("synthesis") or {}).get("takeaways") if m.group(1) == "TK"
               else (rv.meta.get("limitations") or {}).get("open_problems")) or []
        return 1 <= int(m.group(2)) <= len(lst)
    m = re.fullmatch(r"(U|CN|G|X|VD)\d+", ref)          # the review report's items (merge-report numbers them)
    if m:
        return any(isinstance(x, dict) and x.get("id") == ref for x in _report_items(rv, m.group(1)))
    return False


def _pres_pick(rows: list[dict], group=None) -> list[dict]:
    """The KPI rows, then the others by papers, at most 2 of one group while other groups wait (the KPI rows do not
    count: retrieval-confidence's KPI sets would have kept out Natural Questions, 20 papers, for MSR-VTT, 3). A row
    of one paper never takes a place from a bigger one (dashcam: two self-collected sets over Oxford RobotCar)."""
    keep, per = [r for r in rows if r.get("kpi")][:PRES_SUMMARY_ROWS], {}
    for second_pass in (False, True):
        for r in rows:
            g = group(r) if group else None
            if len(keep) < PRES_SUMMARY_ROWS and r not in keep and (second_pass or (per.get(g, 0) < 2 and r["papers"] >= 2)):
                keep.append(r)
                per[g] = per.get(g, 0) + 1
    return sorted(keep, key=lambda r: (-r["papers"], r["id"]))


def _pres_summaries(rv: Review) -> dict:
    """What the included papers are tested on, for the summary scenes: the tasks, the test sets and the metrics, each
    with its number of included papers, most first, at most PRES_SUMMARY_ROWS rows. A paper counts for a test set or a
    metric through its own rows only (not the baselines it re-reports), and for a task through its task_ids or its own
    rows. A test set is the row's dataset, else its benchmark's one dataset, else the benchmark. A KPI's test set and
    metric are always shown, and at most 2 other test sets of a task while other tasks wait (llm-to-domain-slm v1: six
    of the top eight were zero-shot commonsense sets, and only one domain test set showed). The video draws these rows and every
    writer's packet carries them, so each count the narration says is one the screen shows."""
    T, papers = rv.tables, rv.tables["papers"]
    inc = {pid for pid, p in papers.items() if needs_details(p)}
    bench_ds = {bid: (b.get("dataset_ids") or [None])[0] if len(b.get("dataset_ids") or []) == 1 else None
                for bid, b in T["benchmarks"].items()}
    kpi_sets, kpi_metrics = set(), set()
    for k in rv.meta.get("kpis") or []:
        kpi_metrics.add(k.get("metric_id"))
        kpi_sets |= {k.get("dataset_id"), k.get("benchmark_id")} | set((T["benchmarks"].get(k.get("benchmark_id") or "") or {}).get("dataset_ids") or [])
    task_p: dict = {tid: {pid for pid in inc if tid in (papers[pid].get("task_ids") or [])} for tid in T["tasks"]}
    set_p, met_p, set_met, set_task = {}, {}, {}, {}
    for r in T["results"].values():
        pid = r.get("paper_id")
        if pid not in inc or _is_baseline_row(r):
            continue
        sid = r.get("dataset_id") or bench_ds.get(r.get("benchmark_id")) or r.get("benchmark_id")
        ok_set = (sid in T["datasets"] and T["datasets"][sid].get("status") != "excluded") or sid in T["benchmarks"]
        mid, tid = r.get("metric_id"), r.get("task_id")
        if tid in task_p:
            task_p[tid].add(pid)
        if mid in T["metrics"]:
            met_p.setdefault(mid, set()).add(pid)
        if ok_set:
            set_p.setdefault(sid, set()).add(pid)
            if mid in T["metrics"]:
                set_met.setdefault(sid, {}).setdefault(mid, set()).add(pid)
            if tid in T["tasks"]:
                set_task.setdefault(sid, {}).setdefault(tid, set()).add(pid)
    top = lambda d: sorted((k for k in d if d[k]), key=lambda k: (-len(d[k]), k))
    rec = lambda sid: T["datasets"].get(sid) or T["benchmarks"].get(sid) or {}
    def defn(r, n):
        """The definition's core for a table cell: step-3 definitions carry examples, numbers and survey section notes
        ("e.g. 3046 MB for MobiLlama 0.5B", "(named with BLEU in IV-D5)")."""
        d = r.get("definition")
        d = re.sub(r"\s*\([^()]*\)", "", str((d.get("value") if isinstance(d, dict) else d) or ""))
        d = re.split(r"[,;:]?\s+(?:e\.g\.|i\.e\.|for example|for instance|such as|as in|like)\s|;\s", d)[0].strip().rstrip(".,;:")
        return _lead(d + "." if d else "", n)
    sets = top(set_p)

    def set_tasks(sid):
        """The test set's tasks: its rows' task ids, else its record's, else the task most of its papers work on
        (retrieval-confidence: no dataset had task ids, and every Task cell was empty)."""
        own = top(set_task.get(sid) or {}) or [t for t in rec(sid).get("task_ids") or [] if t in T["tasks"]]
        return own or top({tid: set_p[sid] & task_p[tid] for tid in task_p})


    # a task's main test sets: the ones most of its papers report (a compression paper's rows name the evaluation task)
    task_rows = [{"id": tid, "name": T["tasks"][tid].get("name"), "papers": len(task_p[tid]), "definition": defn(T["tasks"][tid], 100),
                  "test_sets": [rec(x).get("name") for x in sorted((x for x in sets if set_p[x] & task_p[tid]),
                                                                  key=lambda x: (-len(set_p[x] & task_p[tid]), x))[:2]]}
                 for tid in top(task_p)]
    set_rows = [{"id": sid, "name": rec(sid).get("name"), "papers": len(set_p[sid]),
                 "task": next((T["tasks"][t].get("name") for t in set_tasks(sid)), None),
                 "metric": next((T["metrics"][m].get("name") for m in top(set_met.get(sid) or {})), None),
                 "kpi": sid in kpi_sets} for sid in sets]
    hib = {True: "higher is better", False: "lower is better"}
    met_rows = [{"id": mid, "name": T["metrics"][mid].get("name"), "papers": len(met_p[mid]), "definition": defn(T["metrics"][mid], 100),
                 "direction": hib.get(T["metrics"][mid].get("higher_is_better")), "kpi": mid in kpi_metrics} for mid in top(met_p)]
    notes = {"tasks": "tasks with included papers, by number of papers",          # one line above the table
             "benchmarks": "test sets: the KPI ones, then the most used, at most 2 more per task",
             "metrics": "metrics: the KPI ones, then the most used"}
    out = {}
    for k, rows, group in (("tasks", task_rows, None), ("benchmarks", set_rows, lambda r: r["task"]), ("metrics", met_rows, None)):
        shown = _pres_pick(rows, group)
        out[k] = {"rows": shown, "total": len(rows), "papers": len(inc), "note": f"{len(shown)} of {len(rows)} {notes[k]}"}
    val = lambda e: e.get("value") if isinstance(e, dict) else e
    for r in out["benchmarks"]["rows"]:         # section 5: a benchmark is a task and rules on a dataset; the facts that
        d = T["datasets"].get(r["id"]) or {}     # say how close a test set is to the target (the packets, not the screen)
        r["benchmark"] = r["id"] in T["benchmarks"]
        r["facts"] = {k: _trim(val(d.get(f)), 90) for k, f in (("size", "size"), ("origin", "real_synthetic"),
                                                                ("access", "availability"), ("limitations", "limitations")) if val(d.get(f))}
    for r in out["metrics"]["rows"]:
        lim = val(T["metrics"][r["id"]].get("limitations"))
        if lim:
            r["limitations"] = _trim(lim, 140)
    return out


_PRES_SOURCE = {"arxiv": "arXiv", "openalex": "OpenAlex", "semanticscholar": "Semantic Scholar", "s2": "Semantic Scholar"}
_PRES_RUN_KIND = {"keyword": "keyword searches", "citing": "citing-paper walks", "reference": "reference walks",
                  "coverage": "coverage checks"}


def _pres_search(rv: Review) -> dict:
    """The search log of the method section (3): where and when the review searched, with which terms and citation walks,
    by which criteria, and the coverage gaps it knows of. Computed from the runs and terms, never from the purpose."""
    runs, terms, P = list(_vals(rv.tables["search_runs"])), list(_vals(rv.tables["search_terms"])), rv.tables["papers"]
    eb, scope, inc = _evidence_base(rv), rv.meta.get("scope") or {}, {pid for pid, p in P.items() if p.get("status") == "included"}
    src: dict = {}
    for r in runs:
        s = _PRES_SOURCE.get(str(r.get("source") or "").lower(), r.get("source") or "other")
        k = _PRES_RUN_KIND.get(r.get("kind"), r.get("kind") or "other")
        src.setdefault(s, {})[k] = src.setdefault(s, {}).get(k, 0) + 1
    dates = sorted(str(r["executed_on"])[:10] for r in runs if r.get("executed_on"))
    done = [t for t in terms if t.get("status") == "searched"]
    best = sorted(done, key=lambda t: (-len(inc & set(t.get("discovered_paper_ids") or [])), t.get("id") or ""))
    kinds = {k: sum(1 for r in runs if r.get("kind") == k) for k in ("reference", "citing", "coverage")}
    flow = {s["stage"]: s["n"] for s in eb["flow"]}
    return {"sources": [{"name": s, "runs": sum(ks.values()), "kinds": [{"kind": k, "n": n} for k, n in sorted(ks.items(), key=lambda x: -x[1])]}
                        for s, ks in sorted(src.items(), key=lambda x: -sum(x[1].values()))],
            "first_search": dates[0] if dates else None, "last_search": dates[-1] if dates else None,
            "terms": {"run": len(done), "total": len(terms), "not_run": len(terms) - len(done),
                      "examples": [t.get("term") for t in best[:6] if t.get("term")]},
            "walks": {"reference": kinds["reference"], "citing": kinds["citing"], "max_hop": eb["max_snowball_hop"],
                      "converged": eb["snowball_converged"], "coverage_check": eb["coverage_check_run"],
                      "surveys": len(eb["surveys_used"])},
            "criteria": {"in": len(scope.get("in") or []), "out": len(scope.get("out") or []),
                         "protocol": len(scope.get("protocol") or []), "year_window": eb["year_window"], "paper_cap": eb["paper_cap"]},
            "gaps": {"no_text": flow.get("text", 0), "over_cap": flow.get("cap", 0), "never_screened": eb["candidates_left"],
                     "terms_not_run": len(terms) - len(done)},
            "found": flow.get("found", 0), "included": flow.get("included", 0),
            "kind": "selective: keyword searches, citation walks, snowballing and a coverage check; not a systematic review"}


def _pres_provenance(rv: Review, ctx: "_PresCtx") -> list[dict]:
    """Where each KPI board's numbers come from (section 7): reported by the paper or verified on an archived
    leaderboard. None is reproduced: a review reads papers and runs nothing."""
    R = rv.tables["results"]
    out = []
    for k in rv.meta.get("kpis") or []:
        prov = [str((R.get(e["result_id"]) or {}).get("provenance") or "") for e in ctx.board(k["id"])]
        out.append({"id": k["id"], "label": _kpi_short(rv, k), "papers": len(prov),
                    "reported": sum(p != "leaderboard-verified" for p in prov),
                    "verified": sum(p == "leaderboard-verified" for p in prov), "reproduced": 0})
    return out


PRES_STANDING_TOP = 2          # the leaders each comparison shows beside the family's members


def _pres_standing(rv: Review, ctx: "_PresCtx", cid: str) -> dict:
    """Where one family's members rank on each KPI comparison (the first scene of its section-6 chapter): the leaders,
    then the family's members in rank order, each with its rank of the comparison's papers and its value on the
    comparison's scale. A rank compares the highest reported scores, whatever the base model and setting."""
    mem = set(rv.tables["clusters"][cid].get("paper_ids") or [])
    in_c = {p: c["id"] for c in rv.tables["clusters"].values() for p in c.get("paper_ids") or []}
    many = len(rv.meta.get("kpis") or []) > 3
    cap = 5 if many else 7                                    # rows a column holds at two or one row of comparisons
    boards, placed = [], set()
    for k in rv.meta.get("kpis") or []:
        board = ctx.board(k["id"])
        in_pct = _board_in_percent(board)
        fam = [e for e in board if e["paper_id"] in mem]
        placed.update(e["paper_id"] for e in fam)
        top = board[:PRES_STANDING_TOP]
        keep = {e["paper_id"] for e in top} | {e["paper_id"] for e in fam[:max(1, cap - PRES_STANDING_TOP)]}
        rows = [{"paper_id": e["paper_id"], "name": ctx.name(e["paper_id"]), "rank": e["rank"], "mine": e["paper_id"] in mem,
                 "cluster": in_c.get(e["paper_id"]), "value": _shown_text(e, in_pct) or e.get("raw_value")}
                for e in board if e["paper_id"] in keep]
        boards.append({"id": k["id"], "label": _kpi_short(rv, k), "of": len(board),
                       "best": fam[0]["rank"] if fam else None, "members": len(fam),
                       "more": max(0, len(fam) - (cap - PRES_STANDING_TOP)), "rows": rows})
    ranked = {r["paper_id"] for r in ctx.rp["ranked"] + ctx.rp["below_cut"]}
    return {"cluster": cid, "name": rv.tables["clusters"][cid].get("name"), "members": len(mem),
            "on_comparisons": len(placed), "without_result": len(mem - ranked), "boards": boards}


def _pres_weak_points(rv: Review, ctx: "_PresCtx") -> dict:
    """Each family's best paper with the kinds of weak point its critique found (section 7): the reviewer's reading
    (inferred), never the authors' words."""
    rows, tot = [], {}
    for c in sorted(rv.tables["clusters"].values(), key=lambda c: c["id"]):
        n: dict = {}
        for w in (ctx.X.get(c["id"]) or {}).get("weak_points") or []:
            if w.get("value"):
                n[w.get("kind") or "other"] = n.get(w.get("kind") or "other", 0) + 1
        if n:
            wp = rv.tables["papers"].get(c.get("winner_paper_id") or "") or {}
            rows.append({"id": c["id"], "name": c.get("name"), "winner": ctx.name(wp["id"]) if wp else None, "kinds": n})
            for k, x in n.items():
                tot[k] = tot.get(k, 0) + x
    return {"kinds": sorted(tot, key=lambda k: (-tot[k], k)), "rows": rows}


# loaders that run a Hugging Face model with no code of the paper's own
HF_STANDARD_LOADERS = {"transformers", "diffusers", "sentence-transformers", "peft", "timm", "open_clip", "gguf", "mlx", "vllm",
                       "llama.cpp", "transformers.js", "keras", "onnx", "safetensors", "adapter-transformers"}


def _pres_readiness(av: dict, ck_value: dict | None = None) -> str:
    """Section 8's compact assessment. Two questions, answered apart (an outside review of the first 30-minute video:
    'Meerkat would have to be rebuilt' was said of a model whose weights are on Hugging Face): can a team evaluate the
    published model (the authors' weights, with code or a standard loader), and can it retrain or adapt it (code)."""
    code, ck = av["code"]["availability"], av["checkpoint"]
    weights = ck["availability"] == "yes" and ck["kind"] != "third-party"
    loader = (ck_value or {}).get("library") in HF_STANDARD_LOADERS
    if weights and (code == "yes" or loader):
        return "evaluate now; code found" if code == "yes" else "evaluate now; no code found"
    if weights:
        return "weights only: find how to run them"
    if code == "yes":
        return "code only: train first"
    return "substantial implementation needed" if code == "no" else "insufficient information"


def _params_text(n) -> str | None:
    return None if not isinstance(n, (int, float)) or n <= 0 else f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.0f}M"


def _size_range(r) -> str | None:
    """'1.5B-32.8B' for the authors' releases of several sizes."""
    if not (isinstance(r, list) and len(r) == 2):
        return None
    lo, hi = _params_text(r[0]), _params_text(r[1])
    return f"{lo}\u2013{hi}" if lo and hi else None


def _pres_feasibility(rv: Review, ctx: "_PresCtx", roadmap: dict | None = None) -> list[dict]:
    """The papers a team would try first (each family's best paper, then the report's roadmap baseline and shortlist),
    best ranked first, with code and the authors' weights checked separately (section 8)."""
    T = rv.tables
    rm = roadmap if roadmap is not None else (rv.meta.get("report") or {}).get("roadmap") or {}
    pos = {r["paper_id"]: r["position"] for r in ctx.rp["ranked"]}
    in_c = {pid: c["id"] for c in T["clusters"].values() for pid in c.get("paper_ids") or []}
    ids = [c.get("winner_paper_id") for c in sorted(T["clusters"].values(), key=lambda c: c["id"])]
    ids += [x.get("paper_id") for x in [rm.get("baseline") or {}] + list(rm.get("shortlist") or []) if isinstance(x, dict)]
    ids = sorted(dict.fromkeys(i for i in ids if i in T["papers"]), key=lambda i: (pos.get(i, 10 ** 6), i))[:8]   # 8 rows of 2 lines fit the frame
    word = {"official": "authors'", "third-party": "third-party"}
    rows = []
    for pid in ids:
        p, av = T["papers"][pid], _availability(T["papers"][pid])
        c, k = av["code"], av["checkpoint"]
        ckv = (p.get("checkpoint") or {}).get("value") or {}
        rows.append({"id": pid, "short": ctx.name(pid), "year": p.get("year"), "venue": published_venue_label(p),
                     "cluster": in_c.get(pid), "code": (word.get(c["kind"], "found") if c["availability"] == "yes" else
                                                        "none" if c["availability"] == "no" else "not found"),
                     "weights": ("authors'" if k["availability"] == "yes" and k["kind"] != "third-party" else
                                 "third-party only" if k["availability"] == "yes" else "none" if k["availability"] == "no" else "not found"),
                     # the authors' HF model card: its licence and size (enrich, hf_model_facts); not found -> null
                     "licence": ckv.get("license") if k["kind"] != "third-party" else None,
                     "size": (_size_range(ckv.get("params_range")) or _params_text(ckv.get("params"))) if k["kind"] != "third-party" else None,
                     "readiness": _pres_readiness(av, ckv)})
    return rows


def _pres_dims(p: dict) -> dict:
    """One paper's five method dimensions (section 6), from its step-5 notes."""
    n = _method_notes(p)
    return {f: _lead(n[f]["value"], 170) for f in PRES_DIMENSIONS if f in n and n[f].get("value")}


def _pres_ranking(rv: Review, ctx: "_PresCtx") -> list[dict]:
    papers = rv.tables["papers"]
    in_c = {pid: c["id"] for c in rv.tables["clusters"].values() for pid in c.get("paper_ids") or []}
    return [{"position": r["position"], "paper_id": r["paper_id"], "title": papers[r["paper_id"]].get("title"),
             "year": papers[r["paper_id"]].get("year"), "venue": r.get("venue"), "cluster_id": in_c.get(r["paper_id"]),
             "best_kpi": r["best_kpi"], "best_rank": r["best_rank"], "denominator": r["denominator"],
             "per_kpi": {k: {"rank": e["rank"], "denominator": e["denominator"], "value": e.get("raw_value"),
                             "units": e.get("units"), "result_id": e["result_id"]} for k, e in r["per_kpi"].items()}}
            for r in ctx.rp["ranked"]]


# --------------------------------------------------------------------------
# step 7: the review report - the judgement the ten sections need beyond the tables (hub tab "Report"; step 8 too)
# --------------------------------------------------------------------------

REPORT_GAP_TYPES = {"method-limitation": "Method limitation", "evidence-gap": "Evidence gap",
                    "engineering-gap": "Engineering gap", "research-opportunity": "Research opportunity"}
REPORT_CONDITION_KINDS = ("works", "unresolved")
# key: (id prefix, (fewest, most) items); unmeasured is section 5, conditions section 7, gaps section 9
REPORT_LISTS = {"unmeasured": ("U", (1, 5)), "conditions": ("CN", (2, 6)), "gaps": ("G", (3, 8))}
# Section 1's top-down verdict (the user, 2026-10-28: "start with top down summary (which is better on what principles)
# before getting into details for each family"): 2-6 rows, each a goal of the purpose, the approach that serves it
# better, the principle that makes it better, when that holds and how well the evidence supports it. The first video
# said "the review offers conditions, not a ranking" and left the viewer without an answer. Ids VD1...: a bare V2 is a
# model version ("Depth Anything V2").
REPORT_VERDICT_N = (2, 5)      # 5 rows at their longest fill a video frame
REPORT_VERDICT_FIELDS = ("goal", "choice", "principle", "condition")
REPORT_SUPPORT = {"consistent": "consistent across comparisons", "few": "a few papers", "conflicting": "conflicting results",
                  "untested": "not tested"}
_REPORT_ITEM_KEYS = {"U": "unmeasured", "CN": "conditions", "G": "gaps", "VD": "verdict"}


def _report_items(rv: Review, prefix: str) -> list:
    """The stored report's numbered items with one id prefix (U, CN, G, VD; X for the roadmap's experiments)."""
    r = rv.meta.get("report") or {}
    lst = (r.get("roadmap") or {}).get("experiments") if prefix == "X" else r.get(_REPORT_ITEM_KEYS[prefix])
    return [x for x in lst or [] if isinstance(x, dict)]
# "no work exists" claims more than a selective review can see; the spec: "The reviewed studies do not establish..."
REPORT_OVERCLAIM = re.compile(r"\b(?:no (?:prior |existing |other |published )?(?:work|research|study|studies|paper|papers|method|methods)"
                              r" (?:exists?|has|have|addresses|address|looks?)|(?:has|have) never been|nobody|no one has|"
                              r"the literature lacks)\b", re.I)


def _report_dir(rv: Review) -> Path:
    return rv.path / "data" / "report"


def _outside_tables(rv: Review, ctx: "_PresCtx") -> list[dict]:
    """What each family's representative paper evaluates beyond the KPI comparisons: its own rows on other test sets and
    metrics (free-text grading, human ratings, case studies), most rows first. The report said "no evaluation uses expert
    grades of free answers" while Meerkat's own rows held a free-text evaluation (an outside review, 2026-10-26)."""
    T = rv.tables
    on_board = {e["result_id"] for k in rv.meta.get("kpis") or [] for e in ctx.board(k["id"])}
    out = []
    for c in sorted(T["clusters"].values(), key=lambda c: c["id"]):
        pid = c.get("winner_paper_id")
        if pid not in T["papers"]:
            continue
        n: dict = {}
        for rid in T["papers"][pid].get("result_ids") or []:
            r = T["results"].get(rid) or {}
            if rid in on_board or _is_baseline_row(r):
                continue
            tgt = (T["benchmarks"].get(r.get("benchmark_id") or "") or T["datasets"].get(r.get("dataset_id") or "") or {}).get("name")
            met = (T["metrics"].get(r.get("metric_id") or "") or {}).get("name")
            if tgt or met:
                e = n.setdefault((tgt, met), {"on": tgt, "metric": met, "rows": 0, "example": rid})
                e["rows"] += 1
        out.append({"cluster_id": c["id"], "paper_id": pid, "short": ctx.name(pid),
                    "evaluations": sorted(n.values(), key=lambda e: -e["rows"])[:8],
                    **({"text_read": "{read}; the journal version ({published}) is newer and was not read".format(**gap)}
                       if (gap := text_version_gap(rv, pid)) else {})})
    return out


def report_input(rv: Review) -> dict:
    """Packet for the report writer (prompts/review-report.md), from merged step-6 outputs only. The tables give most of
    the ten sections; the writer adds the judgement they cannot: the executive summary, the requirements, what the
    benchmarks leave unmeasured, the conditions under which approaches work, the typed gaps and the roadmap."""
    if not (rv.meta.get("synthesis") or {}).get("takeaways"):
        raise SystemExit("no synthesis: the report needs step 6 finished (synthesis-input -> subagent -> merge-synthesis)")
    ctx, papers, T = _PresCtx(rv), rv.tables["papers"], rv.tables
    summary = _pres_review_summary(rv, ctx)
    val = lambda e: e.get("value") if isinstance(e, dict) else e
    in_c = {pid: c["id"] for c in T["clusters"].values() for pid in c.get("paper_ids") or []}
    caps, scope = rv.meta.get("caps") or {}, rv.meta.get("scope") or {}
    return {"review": {"purpose": rv.meta.get("purpose"), "seed": rv.meta.get("seed"),
                       "scope": {k: scope.get(k) for k in ("in", "out", "protocol", "notes")},
                       "year_window": caps.get("years"), "paper_cap": caps.get("max_papers")},
            "tasks": [{"id": t["id"], "name": t.get("name"), **{f: _trim(val(t.get(f)), 300) for f in
                                                                ("definition", "inputs", "outputs", "relevance") if val(t.get(f))}}
                      for t in sorted(T["tasks"].values(), key=lambda t: t["id"])],
            "kpis": summary["kpis"], "summaries": summary["summaries"], "ranking": _pres_ranking(rv, ctx),
            "clusters": [_pres_cluster(rv, c, ctx) | {"winner_dimensions": _pres_dims(papers[c["winner_paper_id"]])
                                                      if c.get("winner_paper_id") in papers else {}}
                         for c in sorted(T["clusters"].values(), key=lambda c: c["id"])],
            "synthesis": summary["synthesis"], "limitations": summary["limitations"], "decisions": summary["decisions"],
            "search": _pres_search(rv), "provenance": _pres_provenance(rv, ctx), "weak_points": _pres_weak_points(rv, ctx),
            "feasibility": _pres_feasibility(rv, ctx, roadmap={}), "counts": synthesis_counts(rv),
            "outside_tables": _outside_tables(rv, ctx),
            "included": [{"paper_id": pid, "title": _short_title(p), "name": ctx.name(pid), "year": p.get("year"), "cluster_id": in_c.get(pid)}
                         for pid, p in sorted(papers.items()) if p.get("status") == "included"],
            "gap_types": REPORT_GAP_TYPES, "condition_kinds": list(REPORT_CONDITION_KINDS),
            "output_path": str(_report_dir(rv) / "report.json")}


def _pres_texts(x) -> list[str]:
    """Every free-text string in a report block, for the number and style checks."""
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for k, v in x.items() if k not in ("id", "paper_id", "refs", "type", "kind", "basis") for s in _pres_texts(v)]
    if isinstance(x, list):
        return [s for v in x for s in _pres_texts(v)]
    return []


def merge_report(rv: Review) -> dict:
    """Check data/report/report.json and store it in meta.report. Any error leaves the stored report as it was: the hub
    and the video draw its texts, so each number in them must be in the packet, each ref a record of the review, and
    the roadmap's papers included ones."""
    f = _report_dir(rv) / "report.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the report subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{f}: invalid JSON ({e}); nothing changed")
    if not isinstance(data, dict):
        raise SystemExit(f"{f}: expected an object with executive_summary, verdict, requirements, unmeasured, conditions, gaps, roadmap")
    rep: dict = {"errors": [], "warnings": [], "style": []}
    E, W = rep["errors"], rep["warnings"]
    inc = {pid for pid, p in rv.tables["papers"].items() if p.get("status") == "included"}

    def refs_of(x, where: str) -> list:
        keep = []
        for r in (x.get("refs") or []) if isinstance(x, dict) else []:
            (keep.append(r) if _pres_ref_ok(rv, r) else W.append(f"{where}: ref {r!r} is not in this review - dropped"))
        return keep

    out: dict = {"executive_summary": {}, "requirements": {}}
    ex = data.get("executive_summary") if isinstance(data.get("executive_summary"), dict) else {}
    for k, label in PRES_EXEC_KEYS.items():
        x = ex.get(k)
        t = x.get("text") if isinstance(x, dict) else x
        if not str(t or "").strip():
            E.append(f"executive_summary.{k}: needs a text (section 1: {label.lower()})")
            continue
        out["executive_summary"][k] = {"text": str(t).strip(), "refs": refs_of(x, f"executive_summary.{k}")}
        named = paper_names_in(rv, str(t))
        if named:
            W.append(f"executive_summary.{k}: names {', '.join(named)}; the summary names no paper and compares none "
                     "(the user, 2026-10-26): say the approach instead")
    out["verdict"] = report_verdict(rv, data.get("verdict"), rep, refs_of)
    rq = data.get("requirements") if isinstance(data.get("requirements"), dict) else {}
    for k in PRES_REQ_KEYS:
        x = rq.get(k)
        if k == "research_questions":
            qs = [str(q).strip() for q in (x if isinstance(x, list) else [x]) if str(q or "").strip()]
            if not qs:
                E.append("requirements.research_questions: needs 1-5 questions the review must answer (section 2)")
            out["requirements"][k] = qs[:5]
        elif not str(x or "").strip():
            E.append(f"requirements.{k}: needs a text, or \"not stated in the review\" when the purpose and scope do not give it")
        else:
            out["requirements"][k] = str(x).strip()
    for key, (prefix, (lo, hi)) in REPORT_LISTS.items():
        keep = []
        for n, x in enumerate(data.get(key) if isinstance(data.get(key), list) else []):
            t = x.get("text") if isinstance(x, dict) else None
            if not str(t or "").strip():
                W.append(f"{key}[{n}]: no text - dropped")
                continue
            it = {"id": f"{prefix}{len(keep) + 1}", "text": str(t).strip(), "refs": refs_of(x, f"{key}[{n}]")}
            if key == "conditions":
                it["kind"] = x.get("kind") if x.get("kind") in REPORT_CONDITION_KINDS else "works"
            if key == "gaps":
                if x.get("type") not in REPORT_GAP_TYPES:
                    E.append(f"gaps[{n}]: type {x.get('type')!r} is not one of {', '.join(REPORT_GAP_TYPES)}")
                    continue
                it["type"] = x["type"]
                # a gap no reviewed record supports is general knowledge, labelled as such (like the open problems)
                it["basis"] = "reviewed-papers" if any(not re.fullmatch(r"OP\d+", r) for r in it["refs"]) else "outside-knowledge"
                if REPORT_OVERCLAIM.search(it["text"]):
                    W.append(f"{it['id']}: claims more than a selective review can see; write \"The reviewed studies do "
                             f"not establish ...\": {it['text']!r}")
            elif not it["refs"]:
                E.append(f"{key}[{n}]: cite the records it rests on in `refs`: {it['text'][:60]!r}")
            keep.append(it)
        if not lo <= len(keep) <= hi:
            E.append(f"{key}: needs {lo}-{hi} items; got {len(keep)}")
        out[key] = keep
    if out["gaps"] and len({g["type"] for g in out["gaps"]}) < 2:
        W.append("gaps: all of one type; the spec tells method limitations, evidence gaps, engineering gaps and research "
                 "opportunities apart")
    rm = data.get("roadmap") if isinstance(data.get("roadmap"), dict) else {}
    base, short, exps = rm.get("baseline"), rm.get("shortlist") or [], rm.get("experiments") or []
    if not (isinstance(base, dict) and base.get("paper_id") in inc and str(base.get("reason") or "").strip()):
        E.append("roadmap.baseline needs an included paper's id and the reason to establish it first (section 10)")
        base = None
    if not isinstance(short, list) or not 1 <= len(short) <= 4 or any(
            not (isinstance(x, dict) and x.get("paper_id") in inc and str(x.get("reason") or "").strip()) for x in short):
        E.append(f"roadmap.shortlist needs 1-4 entries, each an included paper's id and why it is on the list; got {short!r}")
        short = []
    keep = []
    for n, x in enumerate(exps if isinstance(exps, list) else []):
        if not (isinstance(x, dict) and all(str(x.get(k) or "").strip() for k in ("experiment", "question", "decision", "needs", "criteria"))):
            E.append(f"roadmap.experiments[{n}] needs experiment, question, decision, needs (data and implementation) and criteria")
            continue
        keep.append({"id": f"X{len(keep) + 1}", **{k: str(x[k]).strip() for k in ("experiment", "question", "decision", "needs", "criteria")},
                     "refs": refs_of(x, f"roadmap.experiments[{n}]")})
    if not 2 <= len(keep) <= 5:
        E.append(f"roadmap.experiments needs 2-5 proposed experiments; got {len(keep)}")
    out["roadmap"] = {"baseline": base, "shortlist": short, "experiments": keep}
    allowed = [float(x) for x in _pres_numbers(report_input(rv), set())]
    for k in ("executive_summary", "verdict", "requirements", "unmeasured", "conditions", "gaps", "roadmap"):
        for t in _pres_texts(out.get(k)):
            bad = [m.group().strip() for m in PRES_NUM.finditer(t) if not _number_ok(m, allowed)]
            if bad:
                E.append(f"{k}: {', '.join(bad)} is in no record of the packet: {t!r}")
            _style(rep, k, t)
    if rv.tables["clusters"]:
        W += count_issues(count_context(rv), [(k, t) for k in ("executive_summary", "verdict", "conditions", "gaps")
                                              for t in _pres_texts(out.get(k))])
    if E:
        return rep
    rv.meta["report"] = dict(out, source="data/report/report.json", merged_at=now_iso())
    rep["stored"] = {k: len(v) for k, v in out.items() if isinstance(v, (list, dict))}
    return rep


def report_verdict(rv: Review, rows, rep: dict, refs_of) -> list[dict]:
    """Section 1's top-down verdict: which approach is better for which goal, on what principle (REPORT_VERDICT_N rows).
    Errors and warnings go to `rep`; returns the rows to store, numbered VD1..."""
    E, W, C = rep["errors"], rep["warnings"], rv.tables["clusters"]
    keep: list[dict] = []
    for n, x in enumerate(rows if isinstance(rows, list) else []):
        if not (isinstance(x, dict) and all(str(x.get(k) or "").strip() for k in REPORT_VERDICT_FIELDS)):
            E.append(f"verdict[{n}] needs goal, choice, principle and condition (section 1: which approach is better for "
                     "which goal, and on what principle)")
            continue
        cl = [c for c in x.get("cluster_ids") or [] if isinstance(c, str)]
        bad = [c for c in cl if c not in C]
        if bad:
            E.append(f"verdict[{n}]: cluster_ids not in this review: {', '.join(bad)}")
        if x.get("support") not in REPORT_SUPPORT:
            E.append(f"verdict[{n}]: support {x.get('support')!r} is not one of {', '.join(REPORT_SUPPORT)}")
            continue
        it = {"id": f"VD{len(keep) + 1}", **{k: str(x[k]).strip() for k in REPORT_VERDICT_FIELDS},
              "cluster_ids": [c for c in cl if c in C], "support": x["support"], "refs": refs_of(x, f"verdict[{n}]")}
        if not it["refs"]:
            E.append(f"verdict[{n}]: cite the records it rests on in `refs`: {it['goal'][:60]!r}")
        for k, most in (("goal", 8), ("choice", 8), ("condition", 12), ("principle", 25)):
            if len(it[k].split()) > most:
                W.append(f"{it['id']}.{k}: {len(it[k].split())} words; a row of the verdict table holds about {most}: {it[k]!r}")
        named = paper_names_in(rv, " ".join(it[k] for k in REPORT_VERDICT_FIELDS))
        if named:
            W.append(f"{it['id']}: names {', '.join(named)}; the verdict, like the summary, names no paper: say the approach")
        keep.append(it)
    lo, hi = REPORT_VERDICT_N
    if not lo <= len(keep) <= hi:
        E.append(f"verdict: needs {lo}-{hi} rows (goal, the better approach, the principle, when it holds); got {len(keep)}")
    shown = {c for it in keep for c in it["cluster_ids"]}
    for c in sorted(set(C) - shown) if keep else []:
        W.append(f"verdict: family {c} ({C[c].get('name')}) is in no row; say what it is the better choice for, or give "
                 "it a row that says the evidence shows no advantage for it")
    return keep


def report_warnings(rv: Review, verdict: bool = True) -> list[str]:
    """What makes the stored report stale: a roadmap paper no longer included, a ref that is gone, or none at all; and
    (`verdict`, for `status`) a report merged before v1.35.0, which has no verdict for the video's top-down summary.
    `validate` leaves that one out: the older report is still whole, and merge-report refuses a new one without it."""
    r = rv.meta.get("report")
    if not r:
        return (["review report missing: run report-input -> subagent -> merge-report (the hub's Report tab)"]
                if (rv.meta.get("synthesis") or {}).get("takeaways") else [])
    if verdict and not r.get("verdict"):
        return ["review report has no verdict (section 1's top-down summary since v1.35.0: which approach is better for "
                "which goal, and why): rerun report-input -> subagent -> merge-report"]
    inc = {pid for pid, p in rv.tables["papers"].items() if p.get("status") == "included"}
    rm, out = r.get("roadmap") or {}, []
    gone = [x.get("paper_id") for x in [rm.get("baseline") or {}] + list(rm.get("shortlist") or []) if x.get("paper_id") not in inc]
    if gone:
        out.append(f"report roadmap names papers that are no longer included: {', '.join(map(str, gone))} (rerun report-input -> subagent -> merge-report)")
    refs = [x for k in ("unmeasured", "conditions", "gaps") for it in r.get(k) or [] for x in it.get("refs") or []]
    refs += [x for it in (r.get("executive_summary") or {}).values() for x in it.get("refs") or []]
    refs += [x for it in r.get("verdict") or [] for x in it.get("refs") or []]
    refs += [x for it in rm.get("experiments") or [] for x in it.get("refs") or []]
    bad = sorted({x for x in refs if not _pres_ref_ok(rv, x)})
    if bad:
        out.append(f"report cites records that are gone: {', '.join(bad)} (rerun report-input -> subagent -> merge-report)")
    return out


class _PresCtx:
    """What the visual checks look up, computed once per command."""

    def __init__(self, rv: Review):
        self.rv = rv
        self.X = {x.get("cluster_id"): x for x in rv.tables["explainers"].values()}
        self.rp = rank_papers(rv)
        self._rt = None
        self._sm = None

    def summaries(self) -> dict:
        if self._sm is None:
            self._sm = _pres_summaries(self.rv)
        return self._sm

    def result_tables(self) -> dict:
        if self._rt is None:
            self._rt = explainer_result_tables(self.rv, self.rp)
        return self._rt

    def board(self, kid: str) -> list[dict]:
        return sorted((dict(r["per_kpi"][kid], paper_id=r["paper_id"]) for r in self.rp["ranked"] + self.rp["below_cut"]
                       if kid in r["per_kpi"]), key=lambda e: (e["rank"], e["paper_id"]))

    def name(self, pid: str) -> str:
        """What the video calls a paper: its short title when that is a name, else the method name its own KPI rows
        give (Meerkat's title has no colon, so its short title was "Small language models learn enhanced reasoning
        ski..." on screen while the voice said Meerkat)."""
        p = self.rv.tables["papers"].get(pid) or {}
        st = _short_title(p)
        if not st.endswith("...") and len(st) <= 40:
            return st
        if not hasattr(self, "_mv"):
            self._mv = {}
            for k in self.rv.meta.get("kpis") or []:
                for e in self.board(k["id"]):
                    mv = str((self.rv.tables["results"].get(e["result_id"]) or {}).get("method_variant") or "")
                    mv = mv.split("(")[0].split(" + ")[-1].strip()      # "SparseGPT + MixCal (alpha=0.5)": the paper's is MixCal
                    n = re.split(r"[\s(,;:]|-(?=\d)", mv)[0].strip()
                    # a base model's name is not the paper's ("Gemma 1 7B, 50% Pruned": P1984 has no method name)
                    if 2 < len(n) <= 30 and not re.fullmatch(r"(ours|our|base|model|method|baseline|llama\d*(\.\d)?|gemma\d*|"
                                                             r"qwen[\d.]*|mistral|mixtral|phi\d*|gpt-?\d?|opt|bloom|falcon|t5|bert|"
                                                             r"roberta|pythia|vicuna|alpaca)\*?", n, re.I):
                        self._mv.setdefault(e["paper_id"], n)
        return self._mv.get(pid) or st


def _visual_check(rv: Review, v, ctx: _PresCtx, where: str) -> tuple[list[str], set | None]:
    """(errors, the element ids its reveals may show; None means line indexes of on_screen)."""
    if not isinstance(v, dict) or v.get("kind") not in VISUAL_KINDS:
        k = v.get("kind") if isinstance(v, dict) else v
        return [f"{where}: visual kind {k!r} is not one of {', '.join(VISUAL_KINDS)}"], set()
    k, refs, errs = v["kind"], v.get("refs") or [], []
    if not isinstance(refs, list):
        return [f"{where}: visual refs must be a list"], set()
    unknown = [r for r in refs if not _pres_ref_ok(rv, r)]
    if unknown:
        errs.append(f"{where}: visual refs not in this review: {', '.join(map(str, unknown))}")

    def need(n: int, prefix: str, what: str) -> bool:
        ok = len(refs) == n and all(isinstance(r, str) and re.fullmatch(prefix + r"\d{3,}", r) and _pres_ref_ok(rv, r) for r in refs)
        if not ok:
            errs.append(f"{where}: a {k} visual takes {what}; got {refs}")
        return ok

    C, M = rv.tables["clusters"], rv.meta
    if k in _PRES_NO_REFS:
        if refs:
            errs.append(f"{where}: a {k} visual takes no refs; got {refs}")
        els = {"scope": {"in", "out", "protocol"}, "evidence-flow": {s["stage"] for s in _evidence_base(rv)["flow"]},
               "family-table": set(C), "matrix": {p.get("id") for p in (M.get("limitations") or {}).get("problems") or []},
               "takeaways": {f"TK{i}" for i in range(1, len((M.get("synthesis") or {}).get("takeaways") or []) + 1)},
               "open-problems": {f"OP{i}" for i in range(1, len((M.get("limitations") or {}).get("open_problems") or []) + 1)},
               }.get(k, set())
        if k in PRES_SUMMARY_KINDS:
            els = {r["id"] for r in ctx.summaries()[k]["rows"]}
            if not els:
                errs.append(f"{where}: the review has no {k} with included papers to summarise")
        # the section scenes: a list keeps the order the screen draws them in, for the writers' element_ids
        if k == "executive-summary":
            els = _exec_parts(M.get("report"))
        elif k == "verdict":
            els = [str(x.get("id")) for x in (M.get("report") or {}).get("verdict") or [] if isinstance(x, dict)]
            if M.get("report") and not els:
                errs.append(f"{where}: the review report has no verdict (merged before v1.35.0): rerun report-input -> "
                            "subagent -> merge-report")
        elif k == "requirements":
            els = list(PRES_REQ_KEYS)
        elif k == "search":
            els = ["sources", "terms", "walks", "criteria", "gaps"]
        elif k == "provenance":
            els = [x["id"] for x in M.get("kpis") or []]
            if not els:
                errs.append(f"{where}: the review has no KPI boards to show the provenance of")
        elif k == "weak-points":
            els = [r["id"] for r in _pres_weak_points(rv, ctx)["rows"]]
            if not els:
                errs.append(f"{where}: no family winner has a critique with weak points (step 6 winner critiques)")
        elif k == "feasibility":
            els = [r["id"] for r in _pres_feasibility(rv, ctx)]
            if not els:
                errs.append(f"{where}: no family winner or roadmap paper to assess")
        elif k == "roadmap":
            els = ["baseline", "shortlist"] + [str(x.get("id")) for x in ((M.get("report") or {}).get("roadmap") or {}).get("experiments") or []
                                               if isinstance(x, dict)]
        elif k in REPORT_LISTS:
            els = [str(x.get("id")) for x in (M.get("report") or {}).get(k) or []]
        if k in tuple(REPORT_LISTS) + ("executive-summary", "verdict", "requirements", "roadmap") and not M.get("report"):
            errs.append(f"{where}: a {k} visual draws the review report: run report-input -> subagent -> merge-report")
        return errs, (None if k == "example" else els)
    if k == "claim":
        return errs, None
    if k == "dimensions":
        ok = 1 <= len(refs) <= 3 and all(isinstance(r, str) and r in rv.tables["papers"] for r in refs)
        if not ok:
            errs.append(f"{where}: a dimensions visual takes 1-3 paper ids; got {refs}")
            return errs, []
        dims = {r: _pres_dims(rv.tables["papers"][r]) for r in refs}
        thin = [r for r, d in dims.items() if len(d) < 2]
        if thin:
            errs.append(f"{where}: {', '.join(thin)} has fewer than 2 method notes (data, representation, architecture, "
                        "losses, training) to show")
        return errs, [f for f in PRES_DIMENSIONS if any(f in d for d in dims.values())]
    if k == "taxonomy":
        if any(not (isinstance(r, str) and r in C) for r in refs):
            errs.append(f"{where}: a taxonomy visual takes cluster ids; got {refs}")
        return errs, set(r for r in refs if r in C) or set(C)
    if k == "diagram":
        if not need(1, "C", "one cluster id"):
            return errs, set()
        nodes = ((ctx.X.get(refs[0]) or {}).get("diagram") or {}).get("nodes") or []
        if not nodes:
            errs.append(f"{where}: cluster {refs[0]} has no method diagram")
        return errs, {n.get("id") for n in nodes}
    if k == "figure":
        if need(1, "P", "one paper id"):
            fg = rv.tables["papers"][refs[0]].get("figures")
            if not (is_evidence(fg) and fg.get("status") in POSITIVE and fg.get("value")):
                errs.append(f"{where}: paper {refs[0]} has no extracted figure (run `figures`)")
        return errs, set()
    if k == "results-table":
        if not need(1, "C", "one cluster id"):
            return errs, set()
        rt = ctx.result_tables().get((ctx.X.get(refs[0]) or {}).get("id")) or {}
        rows = rt.get("rows") or []
        if not rows:
            errs.append(f"{where}: cluster {refs[0]} has no comparison table")
        # col1..colN: a reveal that names columns draws only those (D-Pruner's 11 columns while the voice said two values)
        return errs, {r.get("label") for r in rows} | {f"col{i}" for i in range(1, len(rt.get("columns") or []) + 1)}
    if k == "kpi-chart":
        if not need(1, "K", "one KPI id"):
            return errs, set()
        return errs, {e["paper_id"] for e in ctx.board(refs[0])}
    if k == "standing":
        if not need(1, "C", "one cluster id"):
            return errs, []
        if not rv.meta.get("kpis"):
            errs.append(f"{where}: the review has no KPI comparisons to place the family on")
        return errs, [x["id"] for x in rv.meta.get("kpis") or []]
    if k == "compare":
        need(2, "C", "two cluster ids")
        return errs, set(COMPARE_FACETS)
    return errs, set()


def _exec_parts(report) -> list[str]:
    """The executive summary parts a stored report has, in reading order: since v1.35.0 problem, uncertainty and next
    step (the verdict table holds the findings); a report merged before has six."""
    ex = (report or {}).get("executive_summary") or {}
    return [k for k in PRES_EXEC_LABELS if k in ex] or list(PRES_EXEC_KEYS)


def _pres_kind_needed(ctx: "_PresCtx", k: str) -> bool:
    """Whether the review has the data a section's data scene draws (a review with no KPI has no provenance to show)."""
    if k == "verdict":
        return bool((ctx.rv.meta.get("report") or {}).get("verdict"))
    if k in PRES_SUMMARY_KINDS:
        return bool(ctx.summaries()[k]["rows"])
    if k == "provenance":
        return bool(ctx.rv.meta.get("kpis"))
    if k in ("dimensions", "feasibility"):
        return bool(ctx.rv.tables["clusters"])
    if k == "standing":
        return bool(ctx.rv.tables["clusters"] and ctx.rv.meta.get("kpis"))
    return True


def _pres_summary_missing(ctx: "_PresCtx", summarized) -> list[str]:
    """The data scenes of the ten sections (PRES_SECTION_SCENES) the review has data for that no chapter shows."""
    # standing belongs to a family chapter, and the plan merge adds it to each one: a plan with none folded every family
    return [k for k in PRES_DATA_KINDS if k not in summarized and k != "standing" and _pres_kind_needed(ctx, k)]


def _content_words(sent: str) -> frozenset:
    """A sentence's content words, lightly stemmed ("measured" = "measure"), for the repeat check."""
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9'.-]*", str(sent).lower()):
        w = w.strip(".'-")
        if not w or w in _PRES_STOP:
            continue
        for suf in ("ing", "ed", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[:-len(suf)]
                break
        out.add(w[:-1] if w.endswith("e") and len(w) >= 5 else w)        # "routes", "route" -> "rout"
    return frozenset(out)


def _says_again(a: frozenset, b: frozenset, shared: int = PRES_REPEAT_SHARED) -> bool:
    """Whether two sentences (as content words) say the same thing (PRES_REPEAT_SHARED, PRES_REPEAT_SHARE)."""
    inter, m = len(a & b), min(len(a), len(b))
    return bool(m) and ((inter >= shared and inter / m >= PRES_REPEAT_SHARE) or (a == b and m >= 2))


def _claim_text(rv: Review, ref: str, ctx: "_PresCtx") -> str:
    """What a claim record says, for the writers' list of what other chapters state."""
    x = _pres_record(rv, ref, ctx, full=False)
    if ref.startswith("VD"):
        t = f"{x.get('goal')}: {x.get('choice')}, because {x.get('principle')}"
    else:
        t = x.get("text") or x.get("title") or x.get("experiment") or ""
    return _trim(_viewer_text(rv, str(t)), 220)


def _plan_card(c: dict) -> dict:
    return {k: c.get(k) for k in ("id", "title", "question", "start_s", "end_s", "takeaway", "transition")}


def _pres_cluster(rv: Review, c: dict, ctx: _PresCtx) -> dict:
    papers, x = rv.tables["papers"], ctx.X.get(c["id"]) or {}
    wp = papers.get(c.get("winner_paper_id") or "") or {}
    fg = wp.get("figures")
    val = lambda e: e.get("value") if isinstance(e, dict) else e
    return {"id": c["id"], "name": c.get("name"), "definition": val(c.get("definition")),
            "distinguishing_trait": c.get("distinguishing_trait"), "winner": c.get("winner_paper_id"),
            "winner_title": wp.get("title"),
            "members": [{"paper_id": pid, "title": papers[pid].get("title"), "year": papers[pid].get("year")}
                        for pid in c.get("paper_ids") or [] if pid in papers],
            "explainer": {"headline": x.get("headline"), **{f: val(x.get(f)) for f in ("problem", "key_idea", "how_it_works", "vs_siblings")},
                          "facets": {f: val(e) for f, e in (x.get("facets") or {}).items()}},
            "diagram_nodes": [{k: n.get(k) for k in ("id", "label", "kind")} for n in ((x.get("diagram") or {}).get("nodes") or [])],
            "has_figure": bool(is_evidence(fg) and fg.get("status") in POSITIVE and fg.get("value")),
            "has_results_table": bool((ctx.result_tables().get(x.get("id")) or {}).get("rows")) if x else False,
            "winner_weak_points": [{"text": e.get("value"), "kind": e.get("kind")} for e in x.get("weak_points") or [] if e.get("value")],
            "winner_declared_limitations": [e.get("value") for e in x.get("declared_limitations") or [] if e.get("value")]}


def _pres_review_summary(rv: Review, ctx: "_PresCtx | None" = None) -> dict:
    M, T = rv.meta, rv.tables
    syn = (M.get("synthesis") or {}).get("takeaways") or []
    lim = M.get("limitations") or {}
    return {"purpose": M.get("purpose"), "seed": M.get("seed"), "scope": {k: (M.get("scope") or {}).get(k) for k in ("in", "out", "protocol")},
            "tasks": [{"id": t["id"], "name": t.get("name")} for t in sorted(T["tasks"].values(), key=lambda t: t["id"])],
            "kpis": [{"id": k["id"], "label": _kpi_label(rv, k), "rationale": k.get("rationale")} for k in M.get("kpis") or []],
            "synthesis": [{"id": f"TK{i}", "text": t.get("text"), "cluster_ids": t.get("cluster_ids") or [],
                           "paper_ids": [e.get("paper_id") for e in t.get("evidence") or []]} for i, t in enumerate(syn, 1)],
            "limitations": {"problems": [{"id": p.get("id"), "title": p.get("title"), "kind": p.get("kind"), "text": p.get("text"),
                                          "cells": {cid: {"state": e.get("state"), "basis": e.get("basis")} for cid, e in (p.get("cells") or {}).items()}}
                                         for p in lim.get("problems") or []],
                            "open_problems": [{"id": f"OP{i}", "title": o.get("title"), "text": o.get("text"), "basis": o.get("basis")}
                                              for i, o in enumerate(lim.get("open_problems") or [], 1)]},
            "adjacent": [{"id": t.get("id"), "name": t.get("name"), "papers": len(t.get("paper_ids") or [])}
                         for t in (M.get("adjacent") or {}).get("topics") or []],
            "decisions": [{"id": d.get("id"), "kind": d.get("kind"), "decision": d.get("decision")} for d in M.get("decisions") or []],
            "summaries": (ctx or _PresCtx(rv)).summaries()}


def presentation_input(rv: Review) -> dict:
    """Packet for the chapter-plan writer (prompts/presentation-plan.md), from merged step-6 outputs only."""
    if not (rv.meta.get("synthesis") or {}).get("takeaways"):
        raise SystemExit("no synthesis: step 8 needs step 6 finished (synthesis-input -> subagent -> merge-synthesis)")
    if not rv.meta.get("report"):
        raise SystemExit("no review report: step 8 draws the executive summary, requirements, gaps and roadmap from it "
                         "(report-input -> subagent -> merge-report, step 7)")
    if not rv.meta["report"].get("verdict"):
        raise SystemExit("the review report has no verdict: the video opens with it (which approach is better for which "
                         "goal, and why; since v1.35.0): rerun report-input -> subagent -> merge-report (step 7)")
    pres = rv.meta.get("presentation") or {}
    minutes = int(pres.get("minutes") or PRES_DEFAULT_MINUTES)
    ctx, papers = _PresCtx(rv), rv.tables["papers"]
    summary = _pres_review_summary(rv, ctx)
    return {"target": {"minutes": minutes, "seconds": minutes * 60, "audience": pres.get("audience") or PRES_DEFAULT_AUDIENCE,
                       "wpm": PRES_WPM, "allocation": pres_allocation(minutes), "min_chapter_s": PRES_MIN_CHAPTER_S,
                       "max_predictions": max(1, round(3 * minutes / 20))},
            "review": {k: summary[k] for k in ("purpose", "seed", "scope", "tasks")}
                      | {"year_window": (rv.meta.get("caps") or {}).get("years"), "paper_cap": (rv.meta.get("caps") or {}).get("max_papers")},
            "evidence_base": _evidence_base(rv), "kpis": summary["kpis"], "summaries": summary["summaries"],
            "ranking": _pres_ranking(rv, ctx),
            "clusters": [_pres_cluster(rv, c, ctx) | {"winner_dimensions": _pres_dims(papers[c["winner_paper_id"]])
                                                      if c.get("winner_paper_id") in papers else {}}
                         for c in sorted(rv.tables["clusters"].values(), key=lambda c: c["id"])],
            "synthesis": summary["synthesis"], "limitations": summary["limitations"], "adjacent": summary["adjacent"],
            "decisions": summary["decisions"],
            # the ten review sections and the data scenes each must show; what the data scenes draw
            "sections": {str(n): {"name": nm, "question": q, "data_scenes": list(PRES_SECTION_SCENES.get(n, ()))}
                         for n, (nm, q) in PRES_SECTIONS.items()},
            "data_scene_min_s": _pres_scene_min_s(minutes),
            "report": _pres_report(rv),
            "search": _pres_search(rv), "provenance": _pres_provenance(rv, ctx), "weak_points": _pres_weak_points(rv, ctx),
            "feasibility": _pres_feasibility(rv, ctx),
            "visual_kinds": {k: {"refs": r, "reveal": e} for k, (r, e) in VISUAL_KINDS.items()},
            "output_path": str(_pres_dir(rv) / "plan.json")}


def _pres_report(rv: Review) -> dict:
    """The stored review report (meta.report) without its bookkeeping."""
    return {k: v for k, v in (rv.meta.get("report") or {}).items() if k not in ("source", "merged_at")}


def merge_presentation_plan(rv: Review) -> dict:
    """Check data/presentation/plan.json and store it. Any error leaves the stored plan as it was."""
    f = _pres_dir(rv) / "plan.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the chapter-plan subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{f}: invalid JSON ({e}); nothing changed")
    if not isinstance(data, dict) or not isinstance(data.get("chapters"), list) or not data["chapters"]:
        raise SystemExit(f'{f}: expected {{"chapters": [...], ...}}; nothing changed')
    minutes = int((rv.meta.get("presentation") or {}).get("minutes") or PRES_DEFAULT_MINUTES)
    rep: dict = {"chapters": 0, "errors": [], "warnings": [], "script_dropped": []}
    E, W = rep["errors"], rep["warnings"]
    if data.get("schema") != PRES_PLAN_SCHEMA:
        E.append(f"plan.json has schema {data.get('schema')!r}: since v1.34.0 a plan covers the ten review sections "
                 f"(schema {PRES_PLAN_SCHEMA}, chapters with `sections`, not the eight `functions`); rerun "
                 "presentation-input and the plan writer")
        return rep
    if not rv.meta.get("report"):
        E.append("no review report: run report-input -> subagent -> merge-report (step 7) before the plan")
        return rep
    target, ctx = minutes * 60, _PresCtx(rv)
    scene_min = _pres_scene_min_s(minutes)
    chapters, seen, prev_end, funcs, clustered, predictions, summarized = [], set(), 0, set(), set(), 0, set()
    kind_secs: dict = {}
    owners: dict = {}         # claim id -> the chapters that state it (one each)
    for i, c in enumerate(data["chapters"]):
        if not isinstance(c, dict) or not re.fullmatch(r"CH\d{2}", str(c.get("id") or "")):
            E.append(f"chapter {i}: needs an id like CH01")
            continue
        cid = c["id"]
        if cid in seen:
            E.append(f"{cid}: duplicate chapter id")
        seen.add(cid)
        s, e = c.get("start_s"), c.get("end_s")
        if not all(isinstance(x, (int, float)) for x in (s, e)):
            E.append(f"{cid}: start_s and end_s must be numbers of seconds")
            continue
        if s != prev_end:
            E.append(f"{cid} starts at {s} s; the previous chapter ends at {prev_end} s (chapters must be contiguous from 0)")
        if e - s < PRES_MIN_CHAPTER_S:
            E.append(f"{cid}: {e - s} s is under the {PRES_MIN_CHAPTER_S} s minimum")
        prev_end = e
        fs = [x for x in c.get("sections") or [] if isinstance(x, int) and x in PRES_SECTIONS]
        if not fs:
            E.append(f"{cid}: sections must list 1-10 (the review sections this chapter covers)")
        funcs.update(fs)
        cl = c.get("cluster_ids") or []
        bad = [x for x in cl if x not in rv.tables["clusters"]]
        if bad:
            E.append(f"{cid}: cluster_ids not in this review: {', '.join(map(str, bad))}")
        clustered.update(x for x in cl if x in rv.tables["clusters"])
        E += _visual_check(rv, c.get("visual"), ctx, cid)[0]
        sm = c.get("summaries") or []
        if not isinstance(sm, list) or any(k not in PRES_DATA_KINDS for k in sm):
            E.append(f"{cid}: summaries must list data scenes from {', '.join(PRES_DATA_KINDS)}; got {sm!r}")
            sm = []
        sm = list(dict.fromkeys(sm))
        if 6 in fs and cl and rv.meta.get("kpis") and "standing" not in sm:
            sm.insert(0, "standing")        # nothing for the writer to choose: every family chapter opens on it
        if 6 in fs and cl and "dimensions" not in sm:
            E.append(f"{cid}: a section-6 chapter about a family lists \"dimensions\" in `summaries` (its best paper's data, "
                     "representation, architecture, losses and training, in a scene of its own)")
        if sm and e - s < scene_min * len(sm):
            E.append(f"{cid}: {e - s} s is too short for {len(sm)} data scene(s) ({', '.join(sm)}); give each at least {scene_min} s")
        summarized.update(sm)
        for k in sm:
            kind_secs.setdefault(k, set()).update(fs)
        if PRES_BAD_TITLE.search(str(c.get("title") or "")) or not c.get("title"):
            W.append(f"{cid}: title {c.get('title')!r} names no question or message")
        ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
        clean_ev = {}
        for side in ("supporting", "challenging"):
            keep = []
            for r in ev.get(side) or []:
                (keep.append(r) if _pres_ref_ok(rv, r) else W.append(f"{cid}: evidence ref {r!r} is not in this review - dropped"))
            clean_ev[side] = keep
        eng = c.get("engagement")
        if eng is not None and not (isinstance(eng, dict) and eng.get("kind") in PRES_ENGAGEMENT):
            W.append(f"{cid}: engagement {eng!r} is not one of {', '.join(PRES_ENGAGEMENT)} - dropped")
            eng = None
        predictions += bool(eng and eng.get("kind") == "prediction")
        dd = c.get("deep_dive")
        if dd is not None and not (isinstance(dd, dict) and _pres_ref_ok(rv, dd.get("paper_id")) and dd.get("reason")):
            W.append(f"{cid}: deep_dive {dd!r} needs a paper id in this review and a reason - dropped")
            dd = None
        claims = []
        for x in c.get("claims") or [] if isinstance(c.get("claims"), list) else []:
            if isinstance(x, str) and PRES_CLAIM_ID.fullmatch(x) and _pres_ref_ok(rv, x):
                claims.append(x)
                owners.setdefault(x, []).append(cid)
            else:
                W.append(f"{cid}: claim {x!r} is not a takeaway, problem, report item or verdict row of this review - dropped")
        chapters.append(dict({k: v for k, v in c.items() if k != "functions"}, start_s=s, end_s=e, sections=fs,
                             cluster_ids=[x for x in cl if x in rv.tables["clusters"]],
                             evidence=clean_ev, engagement=eng, deep_dive=dd, summaries=sm, claims=list(dict.fromkeys(claims))))
    if prev_end != target:
        E.append(f"the chapters end at {prev_end} s; the target is {target} s ({minutes} min): times must sum exactly")
    home = {k: n for n, ks in PRES_SECTION_SCENES.items() for k in ks}
    for k in _pres_summary_missing(ctx, summarized):
        E.append(f"no chapter shows the {k} data scene of section {home[k]} ({PRES_SECTIONS[home[k]][0]}): list \"{k}\" in "
                 "the `summaries` of a chapter that carries that section")
    for k, secs in sorted(kind_secs.items()):
        if home[k] not in secs:
            W.append(f"the {k} data scene is on a chapter without section {home[k]} ({PRES_SECTIONS[home[k]][0]})")
    for n in sorted(set(PRES_SECTIONS) - funcs):
        E.append(f"section {n} ({PRES_SECTIONS[n][0]}) is on no chapter")
    if chapters and 1 not in chapters[0]["sections"]:
        W.append("the executive summary (section 1) should open the video: put it on the first chapter")
    if chapters and 10 not in chapters[-1]["sections"]:
        W.append("the recommendations (section 10) should close the video: put them on the last chapter")
    folded = []
    for fc in data.get("folded_clusters") or []:
        if isinstance(fc, dict) and fc.get("cluster_id") in rv.tables["clusters"] and fc.get("reason") and fc.get("into") in seen:
            folded.append(fc)
        else:
            W.append(f"folded_clusters entry {fc!r} needs a cluster id, an existing chapter in `into` and a reason - dropped")
    for cl in sorted(set(rv.tables["clusters"]) - clustered - {fc["cluster_id"] for fc in folded}):
        E.append(f"cluster {cl} is in no chapter's cluster_ids and not in folded_clusters with a reason")
    cap = max(1, round(3 * minutes / 20))
    if predictions > cap:
        W.append(f"{predictions} prediction moments; the guidelines suggest about {cap} for {minutes} min")
    ha = data.get("headline_answer") or []
    if not 1 <= len(ha) <= 3:
        W.append(f"headline_answer has {len(ha)} items; give 1-3 qualified takeaways")
    ex = data.get("recurring_example")
    if isinstance(ex, dict) and ex.get("text") and not ex.get("illustrative") and not [r for r in ex.get("refs") or [] if _pres_ref_ok(rv, r)]:
        W.append("recurring_example cites no record and is not marked illustrative")
    if isinstance(ex, dict) and ex.get("text"):
        exch = [c for c in ex.get("chapters") or [] if isinstance(c, str)]
        ids_ = [c["id"] for c in chapters]
        if not exch:
            W.append("recurring_example has no `chapters`: name the chapter that introduces it and the few where the "
                     "decision it stands for changes")
        elif [c for c in exch if c not in ids_]:
            W.append(f"recurring_example names chapters not in the plan: {', '.join(c for c in exch if c not in ids_)}")
        elif len(exch) > PRES_EXAMPLE_MAX_CHAPTERS:
            W.append(f"recurring_example returns in {len(exch)} chapters; at most {PRES_EXAMPLE_MAX_CHAPTERS} (the first "
                     "30-minute video named its hospital team 18 times)")
    ops = [c.get("opening") for c in chapters]
    if all(o is None for o in ops):
        W.append("no chapter has an `opening`: vary how chapters start (" + ", ".join(PRES_OPENINGS) + ")")
    else:
        bad = [c["id"] for c in chapters if c.get("opening") not in PRES_OPENINGS]
        if bad:
            W.append(f"opening not one of {', '.join(PRES_OPENINGS)}: {', '.join(bad)}")
        rep_ = [chapters[i]["id"] for i in range(1, len(chapters)) if ops[i] and ops[i] == ops[i - 1]]
        if rep_:
            W.append(f"two chapters in a row open the same way: {', '.join(rep_)}")
        if sum(o == "question" for o in ops) > max(2, len(ops) // 3):
            W.append(f"{sum(o == 'question' for o in ops)} of {len(ops)} chapters open with a question; at most a third")
    for x, chs in sorted(owners.items()):
        if len(set(chs)) > 1:
            E.append(f"{x} is a claim of {', '.join(dict.fromkeys(chs))}: one chapter states each claim (the user, "
                     "2026-10-28: \"do not repeat sentences and even subjects/claims\"); keep it on one card, and let the "
                     "others build on it without saying it again")
    if chapters and not owners:
        W.append("no chapter lists `claims`: name on each card the takeaways, problems, report items and verdict rows it "
                 "states, one chapter each, so that no claim is said twice")
    tk = [(c["id"], _content_words(c.get("takeaway") or "")) for c in chapters]
    for i, (a, wa) in enumerate(tk):
        for b, wb in tk[i + 1:]:
            if _says_again(wa, wb):
                W.append(f"{a} and {b} have the same takeaway: give each chapter its own point")
    rep["chapters"] = len(chapters)
    if E:
        return rep
    pres = load_presentation(rv)
    keep = {c["id"] for c in chapters}
    rep["script_dropped"] = sorted(ch for ch in pres.get("script") or {} if ch not in keep)
    pres["script"] = {ch: s for ch, s in (pres.get("script") or {}).items() if ch in keep}
    pres["plan"] = dict({k: v for k, v in data.items() if k not in ("chapters", "folded_clusters")}, chapters=chapters,
                        folded_clusters=folded, minutes=minutes, target_s=target, wpm=PRES_WPM,
                        source="data/presentation/plan.json", merged_at=now_iso())
    _save_presentation(rv, pres)
    return rep


def _pres_numbers(obj, out: set, skip=("start_s", "end_s", "duration_s", "words_budget", "wpm", "id", "result_id",
                                       "paper_id", "cluster_id", "output_path", "source", "url")) -> set:
    """Every number in the records a chapter cites, as normalised strings: what its narration may say."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                _pres_numbers(v, out, skip)
    elif isinstance(obj, list):
        for v in obj:
            _pres_numbers(v, out, skip)
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, (int, float)):
        out.add(_num_text(float(obj)))
    elif isinstance(obj, str):
        for m in PRES_NUM.finditer(obj):
            out.add(_num_text(_num_token(m)))
        # a size with its unit letter ("3.52B", "7B", "1.5x", "70M"): PRES_NUM stops before the letter and kept only
        # "3", so a writer could not say "3.52 billion" (llm-to-domain-slm CH08, ATP's 6.74B -> 3.52B)
        for m in PRES_NUM_UNIT.finditer(obj):
            out.add(_num_text(_num_token(m)))
    return out


def _num_token(m) -> float:
    return float(m.group(1).replace(",", "") + (m.group(2) or ""))


def _num_text(x: float) -> str:
    return ("%.10f" % x).rstrip("0").rstrip(".")


def _number_ok(m, allowed: list[float]) -> bool:
    x, d = _num_token(m), len((m.group(2) or ".")[1:])
    if not m.group(2) and x <= PRES_SMALL_INT:
        return True
    cands = allowed + ([a * 100 for a in allowed if abs(a) <= 1] if m.group(3) else [])
    return any(abs(round(a, d) - x) < 10 ** -(d + 3) for a in cands)


def _pres_record(rv: Review, ref: str, ctx: _PresCtx, full: bool = True) -> dict:
    """One cited record for a script packet. A family is `full` (diagram, details, results table, members' stated
    limitations) only where the chapter explains it; a chapter that spans many families gets their summaries."""
    T, M = rv.tables, rv.meta
    val = lambda e: e.get("value") if isinstance(e, dict) else e
    if ref in T["clusters"]:
        c, x = T["clusters"][ref], ctx.X.get(ref) or {}
        rt = ctx.result_tables().get(x.get("id")) if x else None
        out = _pres_cluster(rv, c, ctx)
        if not full:
            return out
        out.update({"details": {f: val(e) for f, e in (x.get("details") or {}).items()},
                    "diagram": x.get("diagram"), "glossary": x.get("glossary") or [],
                    "member_limitations": {pid: [val(e) for e in es if val(e)] for pid, es in (x.get("member_limitations") or {}).items()},
                    "results_table": {"caption": (rt or {}).get("why"), "columns": [{k: col.get(k) for k in ("dataset", "metric", "variant", "split")}
                                                                                    for col in (rt or {}).get("columns") or []],
                                      "rows": [{"label": r.get("label"), "own": r.get("own"),
                                                "cells": [None if cl is None else cl.get("value") for cl in r.get("cells") or []]}
                                               for r in (rt or {}).get("rows") or []]} if rt else None})
        return out
    if ref in T["papers"]:
        p = T["papers"][ref]
        pos = next((r for r in ctx.rp["ranked"] + ctx.rp["below_cut"] if r["paper_id"] == ref), None)
        return {"title": p.get("title"), "year": p.get("year"), "venue": published_venue_label(p), "authors": (p.get("authors") or [])[:3],
                "contribution": val(p.get("contribution")), "notes": {f: n.get("value") for f, n in _method_notes(p).items()},
                # `shown`: the value on its board's scale, as the video's chart draws it and the narration says it
                "per_kpi": {k: {"rank": e["rank"], "denominator": e["denominator"], "value": e.get("raw_value"), "units": e.get("units"),
                                "shown": _shown_text(e, _board_in_percent(ctx.board(k)))}
                            for k, e in ((pos or {}).get("per_kpi") or {}).items()},
                "availability": {k: v["availability"] for k, v in _availability(p).items()},
                **({"text_read": "{read}; the journal version ({published}) is newer and was not read".format(**gap)}
                   if (gap := text_version_gap(rv, ref)) else {})}
    if ref in T["results"]:
        r = T["results"][ref]
        tgt = T["benchmarks"].get(r.get("benchmark_id") or "") or T["datasets"].get(r.get("dataset_id") or "") or {}
        on = [(e, b) for b in (ctx.board(k["id"]) for k in M.get("kpis") or []) for e in b if e["result_id"] == ref][:1]
        return {"paper_id": r.get("paper_id"), "on": tgt.get("name"), "metric": (T["metrics"].get(r.get("metric_id") or "") or {}).get("name"),
                **{k: r.get(k) for k in ("metric_variant", "split", "method_variant", "value", "units", "location", "caveats")},
                **({"shown": _shown_text(on[0][0], _board_in_percent(on[0][1]))} if on else {}),
                "spread": result_spread(r)}
    if re.fullmatch(r"K\d{3,}", ref):
        k = next(k for k in M.get("kpis") or [] if k["id"] == ref)
        board = ctx.board(ref)
        return {"label": _kpi_label(rv, k), "rationale": k.get("rationale"),
                "board": [{"paper_id": e["paper_id"], "title": T["papers"][e["paper_id"]].get("title"), "rank": e["rank"],
                           "of": e["denominator"], "value": e.get("raw_value"), "units": e.get("units"),
                           "shown": _shown_text(e, _board_in_percent(board)), "result_id": e["result_id"]} for e in board]}
    for prefix, table in (("T", "tasks"), ("D", "datasets"), ("B", "benchmarks"), ("M", "metrics")):
        if ref in T[table]:
            r = T[table][ref]
            return {"name": r.get("name"), "aliases": r.get("aliases") or [], "definition": val(r.get("definition"))}
    if re.fullmatch(r"L\d+", ref):
        return next(p for p in (M.get("limitations") or {}).get("problems") or [] if p.get("id") == ref)
    if re.fullmatch(r"DEC\d{3,}", ref):
        return next(d for d in M.get("decisions") or [] if d.get("id") == ref)
    if re.fullmatch(r"TK\d+", ref):
        t = M["synthesis"]["takeaways"][int(ref[2:]) - 1]
        return {"text": t.get("text"), "detail": t.get("detail"), "paper_ids": [e.get("paper_id") for e in t.get("evidence") or []]}
    if re.fullmatch(r"OP\d+", ref):
        o = M["limitations"]["open_problems"][int(ref[2:]) - 1]
        return {"title": o.get("title"), "text": o.get("text"), "basis": o.get("basis")}
    if re.fullmatch(r"A\d+", ref):
        t = next(t for t in (M.get("adjacent") or {}).get("topics") or [] if t.get("id") == ref)
        return {"name": t.get("name"), "definition": t.get("definition"), "papers": len(t.get("paper_ids") or [])}
    m = re.fullmatch(r"(U|CN|G|X|VD)\d+", ref)
    if m:
        return next((dict(x) for x in _report_items(rv, m.group(1)) if x.get("id") == ref), {})
    return {}


def _visual_options(rv: Review, card: dict, ctx: _PresCtx) -> list[dict]:
    """Every visual this chapter's script may use, with the element ids its reveals may show."""
    refs = list(dict.fromkeys(card.get("cluster_ids") or [])) + [r for r in (card.get("visual") or {}).get("refs") or [] if r not in (card.get("cluster_ids") or [])]
    cids = [r for r in refs if r in rv.tables["clusters"]]
    cands = [{"kind": k, "refs": []} for k in _PRES_NO_REFS] + [{"kind": "claim", "refs": []}, {"kind": "taxonomy", "refs": []}]
    pos = {r["paper_id"]: r["position"] for r in ctx.rp["ranked"]}
    winners = []
    for cid in cids:
        cands += [{"kind": "standing", "refs": [cid]}, {"kind": "diagram", "refs": [cid]}, {"kind": "results-table", "refs": [cid]}]
        w = rv.tables["clusters"][cid].get("winner_paper_id")
        if w:
            winners.append(w)
            cands += [{"kind": "figure", "refs": [w]}, {"kind": "dimensions", "refs": [w]}]
            # the best paper beside a sibling: how later methods change the idea (section 6)
            sib = sorted((p for p in rv.tables["clusters"][cid].get("paper_ids") or [] if p != w), key=lambda p: (pos.get(p, 10 ** 6), p))
            cands += [{"kind": "dimensions", "refs": [w, p]} for p in sib[:3]]
    if len(winners) > 1:
        cands.append({"kind": "dimensions", "refs": winners[:3]})
    if card.get("deep_dive"):
        cands.append({"kind": "dimensions", "refs": [card["deep_dive"]["paper_id"]]})
    if (card.get("visual") or {}).get("kind") == "dimensions":
        cands.append({"kind": "dimensions", "refs": list((card.get("visual") or {}).get("refs") or [])})
    cands += [{"kind": "compare", "refs": [a, b]} for i, a in enumerate(cids) for b in cids[i + 1:]]
    cands += [{"kind": "kpi-chart", "refs": [k["id"]]} for k in rv.meta.get("kpis") or []]
    seen: set = set()
    cands = [v for v in cands if not ((v["kind"], tuple(v["refs"])) in seen or seen.add((v["kind"], tuple(v["refs"]))))]
    out = []
    for v in cands:
        errs, els = _visual_check(rv, v, ctx, "x")
        if errs:
            continue
        if els is None:
            ids = None
        elif v["kind"] == "diagram":
            ids = [n.get("id") for n in ((ctx.X.get(v["refs"][0]) or {}).get("diagram") or {}).get("nodes") or []]
        elif v["kind"] == "kpi-chart":
            ids = [e["paper_id"] for e in ctx.board(v["refs"][0])]
        elif v["kind"] == "evidence-flow":
            ids = [s["stage"] for s in _evidence_base(rv)["flow"]]
        elif v["kind"] == "compare":
            ids = list(COMPARE_FACETS)
        elif v["kind"] in PRES_SUMMARY_KINDS:
            ids = [r["id"] for r in ctx.summaries()[v["kind"]]["rows"]]
        elif v["kind"] == "results-table":
            rt = ctx.result_tables().get((ctx.X.get(v["refs"][0]) or {}).get("id")) or {}
            cols = {f"col{i}": " ".join(str(x) for x in (c.get("dataset"), c.get("metric"), c.get("variant")) if x)
                    for i, c in enumerate(rt.get("columns") or [], 1)}
            out.append(dict(v, element_ids=[r.get("label") for r in rt.get("rows") or []] + list(cols), columns=cols))
            continue
        else:
            ids = list(els) if isinstance(els, list) else sorted(els, key=str)
        out.append(dict(v, element_ids=ids))
    return out


def _pres_terms(records: dict) -> list[str]:
    """Names a voice may misread: glossary terms, dataset/metric names and mixed-case or digit-bearing tokens."""
    seen: dict[str, None] = {}
    for r in records.values():
        for g in r.get("glossary") or []:
            if isinstance(g, dict) and g.get("term"):
                seen.setdefault(g["term"])
        for k in ("name", "on", "metric"):
            if isinstance(r.get(k), str):
                seen.setdefault(r[k])
        for k in ("title", "winner_title"):
            t = r.get(k)
            if isinstance(t, str) and ":" in t and len(t.split(":")[0].split()) <= 4:
                seen.setdefault(t.split(":")[0].strip())
    blob = json.dumps(records, ensure_ascii=False)
    for m in re.finditer(r"\b(?:[A-Za-z]*[a-z][A-Z][\w-]*|[A-Za-z]+\d[\w-]*|[A-Z]{2,}[\w-]*)\b", blob):
        seen.setdefault(m.group())
    return [t for t in seen if t][:40]


def script_input(rv: Review, chapter: str) -> dict:
    """Packet for one chapter's script writer (prompts/presentation-script.md)."""
    plan = load_presentation(rv).get("plan")
    if not plan:
        raise SystemExit("no merged plan: presentation-input -> plan subagent -> merge-presentation-plan first")
    chs = plan["chapters"]
    i = next((n for n, c in enumerate(chs) if c["id"] == chapter), None)
    if i is None:
        raise SystemExit(f"{chapter} is not in the plan ({', '.join(c['id'] for c in chs)})")
    card, ctx = chs[i], _PresCtx(rv)
    options = _visual_options(rv, card, ctx)
    vrefs = (card.get("visual") or {}).get("refs") or []
    cited = (card.get("evidence") or {}).get("supporting", []) + (card.get("evidence") or {}).get("challenging", [])
    # the records a visual may show come along, except KPI boards the chapter neither cites nor shows (they are big)
    # the chapter's own claims come whole (the `claims` list cuts each text to a line; CH12's writer of the first
    # 20-minute replay could not say U3 and G4 from it)
    refs = list(dict.fromkeys(cited + card.get("cluster_ids", []) + vrefs + list(card.get("claims") or [])
                              + ([card["deep_dive"]["paper_id"]] if card.get("deep_dive") else [])
                              + [r for o in options for r in o["refs"] if not re.fullmatch(r"K\d{3,}", r)]))
    cl = card.get("cluster_ids") or []
    full = set(r for r in vrefs if r in rv.tables["clusters"]) | (set(cl) if len(cl) <= 2 else set())
    records = {r: _pres_record(rv, r, ctx, full=r in full or r not in rv.tables["clusters"]) for r in refs if _pres_ref_ok(rv, r)}
    fs, kind = set(_card_sections(card)), (card.get("visual") or {}).get("kind")
    review = _pres_review_summary(rv, ctx) | {"evidence_base": _evidence_base(rv),
                                         "clusters": [{"id": c["id"], "name": c.get("name"), "members": len(c.get("paper_ids") or [])}
                                                      for c in sorted(rv.tables["clusters"].values(), key=lambda c: c["id"])]}
    if not {2, 3} & fs:
        review.pop("decisions")
    if not {5, 7} & fs:
        review["kpis"] = [{"id": k["id"], "label": k["label"]} for k in review["kpis"]]
    if not ({7, 9} & fs or kind == "matrix"):
        review["limitations"] = {"problems": [{k: p[k] for k in ("id", "title", "kind")} for p in review["limitations"]["problems"]],
                                 "open_problems": [{k: o[k] for k in ("id", "title")} for o in review["limitations"]["open_problems"]]}
    # what the chapter's sections and data scenes draw: the report's parts and the computed section tables
    need, rpt = set(card.get("summaries") or []) | {kind}, _pres_report(rv)
    review["sections"] = [{"n": n, "name": PRES_SECTIONS[n][0], "question": PRES_SECTIONS[n][1],
                           "data_scenes": list(PRES_SECTION_SCENES.get(n, ()))} for n in sorted(fs)]
    for part, secs, kinds in (("executive_summary", {1}, {"executive-summary"}), ("verdict", {1}, {"verdict"}),
                              ("requirements", {2}, {"requirements"}),
                              ("unmeasured", {5}, {"unmeasured"}), ("conditions", {7}, {"conditions"}),
                              ("gaps", {9}, {"gaps"}), ("roadmap", {1, 10}, {"roadmap"})):
        if (secs & fs or kinds & need) and rpt.get(part):
            review.setdefault("report", {})[part] = rpt[part]
    if 3 in fs or "search" in need:
        review["search"] = _pres_search(rv)
    if 7 in fs or need & {"provenance", "weak-points"}:
        review["provenance"], review["weak_points"] = _pres_provenance(rv, ctx), _pres_weak_points(rv, ctx)
    if {8, 10} & fs or "feasibility" in need:
        review["feasibility"] = _pres_feasibility(rv, ctx)
    if "standing" in need:
        review["standing"] = [_pres_standing(rv, ctx, c) for c in card.get("cluster_ids") or [] if c in rv.tables["clusters"]]
    dur = card["end_s"] - card["start_s"]
    allowed = _pres_numbers(records, set()) | _pres_numbers(review, set()) | {str(plan.get("minutes"))}
    # say each thing once: what this chapter states, what the other chapters state, what merged chapters already said
    said = {c: [x for sc_ in e.get("scenes") or [] for x in sc_.get("narration") or []]
            for c, e in _current_scripts(rv, plan).items() if c != chapter}
    return {"target": {"wpm": plan.get("wpm", PRES_WPM), "audience": plan.get("audience"), "minutes": plan.get("minutes")},
            "chapter": dict(card, duration_s=dur), "words_budget": int(dur * PRES_WPM / 60 * PRES_BUDGET),
            "outline": [_plan_card(c) for c in chs],
            "previous": _plan_card(chs[i - 1]) if i else None, "next": _plan_card(chs[i + 1]) if i + 1 < len(chs) else None,
            "headline_answer": plan.get("headline_answer"),
            # introduce: say it once, plainly; revisit: one short return where its decision changes; none: leave it out
            "recurring_example": dict(plan.get("recurring_example") or {}, here=_example_here(plan, chapter)),
            "engagement": card.get("engagement"), "review": review, "records": records,
            "claims": [{"id": x, "text": _claim_text(rv, x, ctx)} for x in card.get("claims") or [] if _pres_ref_ok(rv, x)],
            "claims_elsewhere": [{"chapter": c["id"], "title": c.get("title"),
                                  "claims": [{"id": x, "text": _claim_text(rv, x, ctx)} for x in c.get("claims") or [] if _pres_ref_ok(rv, x)]}
                                 for c in chs if c["id"] != chapter and c.get("claims")],
            "said_elsewhere": said,
            "visual_options": options,
            "allowed_numbers": sorted(allowed, key=lambda s: float(s)), "terms": _pres_terms(records),
            "lexicon": {**(load_presentation(rv).get("lexicon") or {}), **pres_lexicon_fixed(rv)},
            "output_path": str(_pres_dir(rv) / "script" / f"{chapter}.json")}


def paper_names(rv: Review) -> set:
    """Names a viewer would hear for an included paper: short titles of up to 30 characters and the method names its
    own KPI rows give ("Meerkat-8B (ours, ...)" -> Meerkat). For the 'no paper names' rule of the executive summary."""
    T, out = rv.tables, set()
    for p in T["papers"].values():
        if p.get("status") == "included":
            st = _short_title(p)
            if st and len(st) <= 30 and not st.endswith("..."):
                out.add(st)
    for k in rv.meta.get("kpis") or []:
        for e in _PresCtx(rv).board(k["id"]):
            mv = str((T["results"].get(e["result_id"]) or {}).get("method_variant") or "")
            n = re.split(r"[\s(,;:]|-(?=\d)", mv)[0]
            if n.lower() not in ("ours", "base", "model", "llama", "qwen", "mistral", "gemma"):
                out.add(n)
    # a name, not a phrase: an inner capital, a digit or a hyphen (CURE-Med, BioMistral, Fin-R1), or one capitalised word
    # of 6+ letters (Meerkat); "Case Study" and "Code" are short titles too, and plain English
    return {n.strip() for n in out if re.search(r"\w[A-Z0-9-]", n.strip()) and " " not in n.strip()
            or re.fullmatch(r"[A-Z][a-z]{5,}", n.strip())}


def paper_names_in(rv: Review, text: str, names: set | None = None) -> list[str]:
    names = paper_names(rv) if names is None else names
    return sorted(n for n in names if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w])", text or ""))


def _example_here(plan: dict, chapter: str) -> str:
    """'introduce', 'revisit' or 'none': the recurring example's role in one chapter. A plan merged before v1.35.0 has no
    `chapters` list: every chapter may revisit it, as the writers did then."""
    exch = (plan.get("recurring_example") or {}).get("chapters")
    if not (plan.get("recurring_example") or {}).get("text"):
        return "none"
    if not exch:
        return "revisit"
    return "introduce" if chapter == exch[0] else "revisit" if chapter in exch else "none"


_VIEWER_ID_TOKEN = re.compile(rf"(?<![\w-]){_VIEWER_IDS}(?![\w-])")
_NOT_AN_ID = re.compile(r"^(L[0-9])$")       # L1 / L2 are norms and losses as often as limitation ids


def _ids_in_text(rv: Review, text: str) -> list[str]:
    """Record ids in a narration sentence or an on-screen line (the first 30-minute video said 'X2 pits pruning ...' and
    showed 'C005' in viewer text). Only ids this review holds count; L1/L2 followed by loss, norm, distance or error do not."""
    T, M = rv.tables, rv.meta
    out = []
    for m in _VIEWER_ID_TOKEN.finditer(text or ""):
        i = m.group(0)
        if _NOT_AN_ID.match(i) and re.match(r"\s*(loss|norm|distance|error|penalty|regulari)", text[m.end():], re.I):
            continue
        if any(i in T[t] for t in ("clusters", "papers", "results")) or _pres_ref_ok(rv, i):
            out.append(i)
    return out


def _current_scripts(rv: Review, plan: dict, script: dict | None = None) -> dict:
    """The merged chapter scripts that belong to the current plan (a chapter merged before the plan is stale)."""
    sc = load_presentation(rv).get("script") or {} if script is None else script
    keep = {c["id"] for c in plan.get("chapters") or []}
    return {c: e for c, e in sc.items() if c in keep and _ts(e.get("merged_at")) >= _ts(plan.get("merged_at"))}


def _repeat_notes(rv: Review, plan: dict, ch: str, scenes: list[dict], others: dict) -> list[str]:
    """Sentences this chapter says twice or that another chapter already said, and scenes that cite another chapter's
    claim (the plan's `claims`; the executive summary may point to every claim it summarises)."""
    # a standing scene says each family's place in the same frame ("ranks sixth of 45 on ..."): a frame, not a claim
    frame = lambda s: isinstance(s.get("visual"), dict) and s["visual"].get("kind") == "standing"
    out, mine = [], [(s["id"], n, x, _content_words(x)) for s in scenes if not frame(s) for n, x in enumerate(s.get("narration") or [])]
    order = [c["id"] for c in plan.get("chapters") or []]
    k = order.index(ch) if ch in order else -1
    sents = lambda c: [y for s2 in (others.get(c) or {}).get("scenes") or [] if not frame(s2) for y in s2.get("narration") or []]
    seam = {}            # sentence index -> (neighbour chapter, its sentences on that side)
    if k > 0 and order[k - 1] in others:
        seam.update({j: (order[k - 1], sents(order[k - 1])[-PRES_SEAM_SENTENCES:]) for j in range(min(PRES_SEAM_SENTENCES, len(mine)))})
    if 0 <= k < len(order) - 1 and order[k + 1] in others:
        seam.update({j: (order[k + 1], sents(order[k + 1])[:PRES_SEAM_SENTENCES]) for j in range(max(0, len(mine) - PRES_SEAM_SENTENCES), len(mine))})
    for i, (sid, n, x, w) in enumerate(mine):
        near = seam.get(i)
        hit = next((y for y in near[1] if _says_again(w, _content_words(y), PRES_SEAM_SHARED)), None) if near else None
        if hit:
            out.append(f"{sid} sentence {n} says again what {near[0]} said next to it: {x!r} ~ {hit!r} (a chapter's question is "
                       "asked once: where the previous chapter ends on it, start on its answer)")
            continue
        for sid2, m, y, w2 in mine[:i]:
            if _says_again(w, w2):
                out.append(f"{sid} sentence {n} says again what {sid2} sentence {m} said: {x!r} ~ {y!r}")
                break
        else:
            for c2, e in sorted(x for x in others.items() if x[0] != ch):
                hit = next((y for s2 in e.get("scenes") or [] if not frame(s2) for y in s2.get("narration") or []
                            if _says_again(w, _content_words(y))), None)
                if hit:
                    out.append(f"{sid} sentence {n} says again what {c2} said: {x!r} ~ {hit!r}")
                    break
    owner = {x: c["id"] for c in plan.get("chapters") or [] for x in c.get("claims") or []}
    card = next((c for c in plan.get("chapters") or [] if c["id"] == ch), {})
    if 1 not in _card_sections(card):
        for s in scenes:
            other = [f"{x} ({owner[x]})" for x in s.get("sources") or [] if owner.get(x) not in (None, ch)]
            if other:
                out.append(f"{s['id']}: cites {', '.join(other)}, the claim of another chapter: build on it without saying "
                           "it again, or leave it out, and drop it from `sources`")
    return out


def _check_script(rv: Review, plan: dict, f: Path, ctx: _PresCtx, rep: dict, others: dict | None = None) -> tuple[list[str], dict | None, list]:
    ch = f.stem
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        return [f"{f.name}: invalid JSON ({e})"], None, []
    card = next((c for c in plan["chapters"] if c["id"] == ch), None)
    if card is None:
        return [f"{ch} is not in the plan"], None, []
    if not isinstance(data, dict) or data.get("chapter_id", ch) != ch or not isinstance(data.get("scenes"), list) or not data["scenes"]:
        return [f'{f.name}: expected {{"chapter_id": "{ch}", "scenes": [...]}}'], None, []
    E, W = [], rep["warnings"]
    allowed = [float(x) for x in script_input(rv, ch)["allowed_numbers"]]
    wpm, dur = PRES_WPM, card["end_s"] - card["start_s"]
    lex = dict(load_presentation(rv).get("lexicon") or {})
    lex.update({p["term"]: p["say"] for p in data.get("pronunciations") or [] if isinstance(p, dict) and p.get("term") and p.get("say")})
    lex.update(pres_lexicon_fixed(rv))
    # a KPI value said on another scale than its board's chart ("0.0421" for a 4.21% row; MDE CH08/CH09)
    other_scale = {}
    for k in rv.meta.get("kpis") or []:
        b = ctx.board(k["id"])
        pct = _board_in_percent(b)
        for e in b:
            sh = _shown_text(e, pct)
            for x in (e.get("raw_value"), e.get("value")):
                if pct and sh and isinstance(x, (int, float)) and f"{_num_text(float(x))}%" != sh:
                    other_scale.setdefault(_num_text(float(x)), sh)
    scenes, ids, t, words, cx = [], set(), float(card["start_s"]), 0, None
    cx_names = paper_names(rv) if 1 in _card_sections(card) else set()
    for j, s in enumerate(data["scenes"]):
        sid = s.get("id") if isinstance(s, dict) else None
        if not sid:
            E.append(f"{ch} scene {j}: needs an id")
            continue
        if sid in ids:
            E.append(f"{ch}: duplicate scene id {sid}")
        ids.add(sid)
        narr = [x.strip() for x in s.get("narration") or [] if isinstance(x, str) and x.strip()]
        if not narr:
            E.append(f"{sid}: narration must be a list of sentences")
            continue
        claim = s.get("claim_type")
        if claim not in PRES_CLAIMS:
            E.append(f"{sid}: claim_type {claim!r} is not one of {', '.join(PRES_CLAIMS)}")
        srcs = [x for x in s.get("sources") or []]
        bad = [x for x in srcs if not _pres_ref_ok(rv, x)]
        if bad:
            E.append(f"{sid}: unknown source {', '.join(map(str, bad))}")
        if claim == "reported" and not srcs:
            E.append(f"{sid}: a reported scene needs at least one source")
        v = s.get("visual")
        verrs, els = _visual_check(rv, v, ctx, sid)
        E += verrs
        if isinstance(v, dict) and v.get("kind") == "example" and claim != "illustrative":
            E.append(f"{sid}: an example visual needs claim_type illustrative")
        lines = [x for x in str(s.get("on_screen") or "").split("\n") if x.strip()]
        if isinstance(v, dict) and v.get("kind") == "matrix" and len(lines) > 2:
            W.append(f"{sid}: a matrix shows on_screen lines in a strip below it; 3 or more can take a second row and shrink "
                     f"its rows (MDE CH11: 21 px text, under the video's 22 px minimum): keep at most 2 short lines")
        reveal = []
        for rvl in (v.get("reveal") or []) if isinstance(v, dict) else []:
            rvl = rvl if isinstance(rvl, dict) else {}
            at, show = rvl.get("at"), [str(x) for x in rvl.get("show") or []]
            if not isinstance(at, int) or not 0 <= at < len(narr):
                E.append(f"{sid}: reveal at {at} is past the last sentence (0-{len(narr) - 1})")
            ok = {str(n) for n in range(len(lines))} if els is None else {str(e) for e in els}
            missing = [x for x in show if x not in ok]
            if missing:
                E.append(f"{sid}: reveal shows {', '.join(missing)}, which the {(v or {}).get('kind')} visual does not have")
            reveal.append({"at": at, "show": show})
        if isinstance(v, dict) and v.get("kind") == "roadmap":
            xs = {x for r_ in reveal for x in r_["show"] if re.fullmatch(r"X\d+", x)}
            if len(xs) > 3:
                E.append(f"{sid}: a roadmap scene draws at most 3 experiment cards; it names {len(xs)}: give the others a "
                         "scene of their own")
        own = [r for r in srcs + ((v.get("refs") or []) if isinstance(v, dict) else []) if _pres_ref_ok(rv, r)]
        mine = allowed + [float(x) for x in _pres_numbers({r: _pres_record(rv, r, ctx) for r in own}, set())] if own else allowed
        for n, sent in enumerate(narr + lines):
            where = f"{sid} sentence {n}" if n < len(narr) else f"{sid} on_screen"
            bad_n = [m.group().strip() for m in PRES_NUM.finditer(sent) if not _number_ok(m, mine)]
            if bad_n:
                msg = f"{where}: {', '.join(bad_n)} is in no record this chapter cites: {sent!r}"
                (W.append(msg + " (illustrative scene: label it as illustrative on screen)") if claim == "illustrative" else E.append(msg))
            if PRES_NUM_WORDS.search(sent):
                W.append(f"{where}: write numbers as digits so the number check can read them: {sent!r}")
            said = [_num_text(_num_token(m)) for m in PRES_NUM.finditer(sent)]
            for x in dict.fromkeys(x for x in said if "." in x and x in other_scale):
                if other_scale[x].rstrip("%") not in said:        # "0.042 versus 4.2 percent" explains the two scales
                    W.append(f"{where}: {x} is a KPI value on another scale than its board's chart: say {other_scale[x]}: {sent!r}")
        if rv.tables["clusters"]:          # "7 of the 13 ranked papers" copied from an old takeaway (retrieval-confidence CH12)
            cx = cx or count_context(rv)
            W += count_issues(cx, [(f"{sid} sentence {n}", x) for n, x in enumerate(narr)] + [(f"{sid} on_screen", "\n".join(lines))])
        eng = s.get("engagement")
        if eng is not None and not card.get("engagement"):
            W.append(f"{sid}: engagement {eng!r} dropped - the plan assigned {ch} no engagement moment")
            eng = None
        pause = s.get("pause_after_s")
        pause = float(pause) if isinstance(pause, (int, float)) and 0 <= pause <= 5 else PRES_PAUSE_S
        if eng and (eng.get("kind") if isinstance(eng, dict) else None) == "prediction" and pause < PRES_PREDICTION_PAUSE_S:
            W.append(f"{sid}: a prediction moment needs a pause of at least {PRES_PREDICTION_PAUSE_S} s after it")
        tr = s.get("transition") if s.get("transition") in PRES_TRANSITIONS else "cut"
        n_words = sum(len(x.split()) for x in narr)
        spoken = sum(len(_spoken(x, lex).split()) for x in narr)          # "QPP" is said as 3 words: "Q P P"
        est = round(spoken * 60 / wpm + (len(narr) - 1) * PRES_SENT_GAP_S + pause, 1)
        for n, sent in enumerate(narr):
            why = _pres_density(sent)
            if why:
                rep.setdefault("density", []).append(f"{sid} sentence {n}: {why}: {sent!r}")
            for rx, advice in PRES_WORDING:
                if rx.search(sent):
                    rep.setdefault("wording", []).append(f"{sid} sentence {n}: {advice}: {sent!r}")
            if 1 in _card_sections(card) and paper_names_in(rv, sent, cx_names):
                rep.setdefault("wording", []).append(f"{sid} sentence {n}: the executive summary names no paper "
                                                     f"({', '.join(paper_names_in(rv, sent, cx_names))}): say the approach: {sent!r}")
            if PRES_INVENTED.search(sent) and _example_here(plan, ch) != "introduce":
                rep.setdefault("wording", []).append(f"{sid} sentence {n}: the screen's Illustrative badge says it is invented; "
                                                     f"say so only where the example is introduced: {sent!r}")
        for n, sent in enumerate(narr + lines):
            hit = _ids_in_text(rv, sent)
            if hit:
                E.append(f"{sid} {'sentence ' + str(n) if n < len(narr) else 'on_screen'}: record id {', '.join(hit)} in "
                         f"viewer text; say the method, family or experiment by name: {sent!r}")
        words += n_words
        scenes.append({"id": sid, "narration": narr, "visual": dict(v or {}, reveal=reveal) if isinstance(v, dict) else v,
                       "on_screen": s.get("on_screen") or "", "claim_type": claim, "sources": srcs, "pause_after_s": pause,
                       "transition": tr, "engagement": eng, "start_s": round(t, 1), "est_s": est})
        t += est
    kinds = {(x.get("visual") or {}).get("kind") for x in scenes if isinstance(x.get("visual"), dict)}
    if "standing" in (card.get("summaries") or []) and "standing" in kinds and (scenes[0].get("visual") or {}).get("kind") != "standing":
        E.append(f"{ch}: the standing scene (where the family ranks on each comparison) opens the chapter: make it the "
                 "first scene, before the family's mechanism is described")
    for k in card.get("summaries") or []:
        if k not in kinds:
            E.append(f"{ch}: the plan gives this chapter the {k} data scene: give it a scene of its own with a {k} visual "
                     + (f"(visual_options; its rows are in review.summaries.{k})" if k in PRES_SUMMARY_KINDS else "(visual_options)"))
    rep.setdefault("repetition", []).extend(_repeat_notes(rv, plan, ch, scenes, _current_scripts(rv, plan, others)))
    est = sum(x["est_s"] for x in scenes)
    off = (est - dur) / dur if dur else 0
    rep["timing"][ch] = {"est_s": round(est, 1), "duration_s": dur, "words": words, "off": round(off, 3)}
    if abs(off) > PRES_TIMING_ERR:
        E.append(f"{ch}: about {est:.0f} s of speech for a {dur} s chapter ({'too long' if off > 0 else 'too short'} by "
                 f"{abs(off):.0%}; aim for {int(dur * wpm / 60 * PRES_BUDGET)} words, respelled names count as said)")
    elif abs(off) > PRES_TIMING_WARN:
        W.append(f"{ch}: about {est:.0f} s of speech for a {dur} s chapter ({off:+.0%})")
    prons = [p for p in data.get("pronunciations") or [] if isinstance(p, dict) and p.get("term") and p.get("say")]
    entry = {"scenes": scenes, "words": words, "est_s": round(est, 1), "duration_s": dur,
             "source": f"data/presentation/script/{ch}.json", "merged_at": now_iso()}
    return E, entry, prons


def _pres_density(sent: str) -> str | None:
    """Why one narration sentence carries too much for a listener (who cannot re-read it), or None."""
    words, marks, names = len(sent.split()), len(PRES_CLAUSE.findall(sent)), len(PRES_NAME.findall(sent))
    nums = sum(1 for m in PRES_NUM.finditer(sent)
               if m.group(2) or m.group(3) or not (int(m.group(1).replace(",", "")) <= PRES_SMALL_INT
                                                    or 1990 <= int(m.group(1).replace(",", "")) <= 2039))
    load = marks + max(0, names - 1) + max(0, nums - 1)
    if words <= PRES_DENSE_WORDS and load < PRES_DENSE_LOAD:
        return None
    return (f"{words} words, {marks} clause mark(s), {names} name(s), {nums} result number(s): one idea per sentence; "
            "split it or move a part on screen")


def check_script(rv: Review, chapter: str) -> dict:
    """The merge's checks on one chapter file, storing nothing (read-only): script writers run it before they finish."""
    plan = load_presentation(rv).get("plan")
    if not plan:
        raise SystemExit("no merged plan: run merge-presentation-plan first")
    f = _pres_dir(rv) / "script" / f"{chapter}.json"
    if not f.exists():
        raise SystemExit(f"{f} not found")
    rep: dict = {"chapter": chapter, "ok": False, "errors": [], "warnings": [], "density": [], "wording": [], "repetition": [], "timing": {}}
    rep["errors"], entry, _ = _check_script(rv, plan, f, _PresCtx(rv), rep)
    rep["ok"] = not rep["errors"]
    return rep


def pres_lexicon_fixed(rv: Review) -> dict:
    """data/presentation/lexicon.json, {term: spoken form}: the orchestrator's settled respellings. They win over every
    writer's entry (parallel writers cannot see each other's: one wrote "F G two" for FG², another "F G squared")."""
    try:
        d = json.loads((_pres_dir(rv) / "lexicon.json").read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in d.items() if isinstance(v, str) and v.strip()} if isinstance(d, dict) else {}


def merge_script(rv: Review, chapter: str | None = None) -> dict:
    """Check each chapter's script (data/presentation/script/CH##.json) and store the ones that pass. Narration is
    spoken English: no ste-style checks run on it; `density` notes list sentences that carry too much for a listener,
    `repetition` notes sentences said before (in the chapter, or in a chapter merged earlier, this call included)."""
    pres = load_presentation(rv)
    plan = pres.get("plan")
    if not plan:
        raise SystemExit("no merged plan: run merge-presentation-plan first")
    d = _pres_dir(rv) / "script"
    files = [d / f"{chapter}.json"] if chapter else sorted(f for f in d.glob("CH*.json") if re.fullmatch(r"CH\d{2}", f.stem)) if d.exists() else []
    if chapter and not files[0].exists():
        raise SystemExit(f"{files[0]} not found; the script subagent for {chapter} writes it")
    rep: dict = {"merged": [], "rejected": {}, "warnings": [], "density": [], "wording": [], "repetition": [], "timing": {}}
    ctx = _PresCtx(rv)
    script, lex = pres.setdefault("script", {}), pres.setdefault("lexicon", {})
    fixed = pres_lexicon_fixed(rv)
    inplan = {c["id"] for c in plan["chapters"]}
    for f in files:
        if not chapter and f.stem not in inplan:       # a 30-minute plan's CH17-CH20 after a 20-minute re-plan
            rep["warnings"].append(f"{f.name}: {f.stem} is not in the plan (a chapter of an earlier plan) - skipped")
            continue
        errs, entry, prons = _check_script(rv, plan, f, ctx, rep, others=script)
        if errs:
            rep["rejected"][f.stem] = errs
            continue
        script[f.stem] = entry
        rep["merged"].append(f.stem)
        for p in prons:
            if p["term"] in fixed:
                continue
            if lex.get(p["term"]) not in (None, p["say"]):
                rep["warnings"].append(f"{f.stem}: pronunciation of {p['term']!r} changed from {lex[p['term']]!r} to {p['say']!r}"
                                       " - settle it in data/presentation/lexicon.json")
            lex[p["term"]] = p["say"]
    changed = {k: v for k, v in fixed.items() if lex.get(k) != v}
    if changed:
        rep["lexicon_fixed"] = changed
    lex.update(fixed)
    if rep["merged"] or changed:
        _save_presentation(rv, pres)
    return rep


def _pres_status_rows(rv: Review, row, log: dict) -> None:
    if not (rv.meta.get("presentation") or {}).get("wanted"):
        return
    pres = load_presentation(rv)
    plan = pres.get("plan")
    late = [c for c in ("merge-clusters", "merge-explainers", "merge-synthesis", "merge-limitations", "merge-report", "kpi")
            if plan and c in log and _ts(log[c]) > _ts(plan.get("merged_at"))]
    old = bool(plan) and int(plan.get("schema") or 1) < PRES_PLAN_SCHEMA
    nosum = (_pres_summary_missing(_PresCtx(rv), {k for c in plan["chapters"] for k in c.get("summaries") or []})
             if plan and not late and not old else [])
    row(8, "plan", "todo" if not plan else ("stale" if late or old else "warn" if nosum else "done"),
        "no chapter plan" if not plan else (f"{', '.join(late)} ran after the plan merge" if late else
                                            "a plan of the first guidelines (8 chapter functions, before v1.34.0): re-plan "
                                            "for the 10 review sections" if old else
                                            f"no chapter shows the data scene(s) {', '.join(nosum)}" if nosum else
                                            f"{len(plan['chapters'])} chapters, {plan.get('minutes')} min"),
        ("report-input -> subagent -> merge-report, then " if not rv.meta.get("report") else "")
        + "presentation-input -> plan subagent -> merge-presentation-plan" if not plan or late or old or nosum else "")
    if not plan:
        row(8, "script", "todo", "needs the plan first", "")
        return
    sc = pres.get("script") or {}
    miss = [c["id"] for c in plan["chapters"] if c["id"] not in sc]
    old = [c["id"] for c in plan["chapters"] if c["id"] in sc and _ts(sc[c["id"]].get("merged_at")) < _ts(plan.get("merged_at"))]
    detail = "; ".join(x for x in (f"no script: {_ids(miss)}" if miss else "", f"older than the plan: {_ids(old)}" if old else "") if x)
    row(8, "script", "todo" if miss else ("stale" if old else "done"), detail or f"{len(sc)} chapter script(s)",
        "script-input --chapter CH## -> script subagent per chapter -> merge-script" if miss or old else "")
    _audio_status_rows(rv, row)
    a, vf = load_audio(rv), _pres_dir(rv) / "video.json"
    if a.get("scenes"):
        try:
            v = json.loads(vf.read_text())
        except (OSError, ValueError):
            v = None
        late = bool(v) and v.get("audio_built_at") != a.get("built_at")
        bad = [k for k, ok in ((v or {}).get("checks") or {}).items() if not ok]
        row(8, "video", "todo" if not v else ("stale" if late else ("warn" if bad else "done")),
            "no video" if not v else ("the audio was rebuilt after the video" if late else
                                      (f"checks failed: {', '.join(bad)}" if bad else f"{v.get('mp4')} ({(v.get('probe') or {}).get('size_mb')} MB)")),
            "video" if not v or late else ("read data/presentation/video.json and fix what it lists" if bad else ""))


# --------------------------------------------------------------------------
# step 8: narration audio (Kokoro TTS + mlx-whisper check, in their own venv; this file stays stdlib-only)
# --------------------------------------------------------------------------

VOICE_HOME_DEFAULT = Path.home() / ".local" / "share" / "litrev" / "tts"
# preshed 3.0.14 (wheels built with Cython 3.3.0, yanked) breaks spaCy's tokenizer, so misaki cannot load en_core_web_sm
# (OverflowError, explosion/preshed#57). Excluding it also repairs a venv that has it: voice-setup then installs 3.0.15.
VOICE_PINS = ("kokoro==0.9.4", "misaki[en]==0.9.4", "soundfile", "mlx-whisper==0.4.3", "preshed!=3.0.14")
VOICE_DEFAULT, VOICE_LANG, VOICE_SR = "af_heart", "a", 24000
ASR_MODEL = "mlx-community/whisper-small.en-mlx"
AUDIO_SENT_GAP_S = 0.3                  # silence between two sentences of one scene
# A scene is flagged when the recogniser's text (letters and digits, spaces dropped) is under 0.90 similar to both
# the spoken and the written form, its last word is not heard, or speech stops for over 1.5 s. Word error rate is
# only reported: on the 10-minute test it was 0.10-0.27 on 10 scenes from spellings alone ("hot pot" / "HotpotQA",
# "thirty 2 b" / "32B") while every scene's character similarity was 0.92 or more.
AUDIO_CHAR_FLAG, AUDIO_GAP_FLAG_S, AUDIO_LOUD_FLAG_LU = 0.90, 1.5, 3.0
AUDIO_CHECK_RULE = 2      # bump when _asr_compare changes: stored checks are recomputed from the text heard
AUDIO_DRIFT_FLAG = 0.10                 # a chapter whose audio is 10% off its planned time: rewrite or re-plan it
_SMALL_NUM = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}


def voice_home() -> Path:
    return Path(os.environ.get("LITREV_VOICE_HOME") or VOICE_HOME_DEFAULT)


def voice_python() -> Path:
    return voice_home() / "bin" / "python"


def voice_setup(warm: bool = True) -> dict:
    """Make the voice venv: Python 3.12 (kokoro has no 3.13 release, kokoro#342), pip seeded (misaki installs its
    spaCy model with pip at first use), pinned packages. `warm` then speaks and hears one sentence, so the models
    (~330 MB Kokoro, ~480 MB whisper small.en) download now and not in the first review's `audio`."""
    import shutil, subprocess, tempfile
    home = voice_home()
    # the Litrev App starts with a short PATH: look where uv's and Homebrew's installers put uv
    uv = shutil.which("uv") or next((str(p) for p in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv",
                                                      Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")) if p.exists()), None)
    if uv:
        subprocess.run([uv, "venv", "--seed", "--allow-existing", "--python", "3.12", str(home)], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(home / "bin" / "python"), *VOICE_PINS], check=True)
    else:
        py = shutil.which("python3.12") or next((p for p in ("/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12") if Path(p).exists()), None)
        if not py:
            raise SystemExit("voice-setup needs `uv` (https://docs.astral.sh/uv/) or python3.12")
        subprocess.run([py, "-m", "venv", str(home)], check=True)
        subprocess.run([str(home / "bin" / "pip"), "install", *VOICE_PINS], check=True)
    out = {"venv": str(home), "python": str(voice_python()), "pins": list(VOICE_PINS), "warm": False}
    if warm:
        with tempfile.TemporaryDirectory(prefix="litrev-voice-") as d:
            w = Path(d)
            r = _run_worker("tts", {"voice": VOICE_DEFAULT, "lang": VOICE_LANG, "speed": 1.0,
                                    "items": [{"id": "warm", "text": "The voice is ready.", "out": str(w / "warm.wav")}]}, w)
            heard = _run_worker("asr", {"model": ASR_MODEL, "items": [{"id": "warm", "wav": str(w / "warm.wav")}]}, w)
        out.update(warm=True, engine=r.get("engine"), heard=heard["items"][0]["text"])
        (home / ".litrev-warm").write_text(now_iso())
    return out


def _spoken(text: str, lex: dict) -> str:
    """The text the voice reads: each lexicon term replaced by its spoken form (captions keep the written form)."""
    for term in sorted(lex, key=len, reverse=True):
        text = re.sub(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", lambda m: lex[term], text)
    return text


def _norm_words(text: str) -> list[str]:
    """Lower-case words, small number words as digits, runs of single letters joined: the lexicon makes the voice say
    "P D Q P P", and the recogniser writes "PDQPP"."""
    t = re.sub(r"(?<=\d),(?=\d{3})", "", str(text).lower().replace("\u2019", "'"))
    out, run = [], False
    for w in re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", t):
        w = _SMALL_NUM.get(w, w)
        single = len(w) == 1 and w.isalpha()
        if single and run:
            out[-1] += w
        else:
            out.append(w)
        run = single
    return out


def _asr_compare(ref: str, hyp_text: str) -> dict:
    """How far what the recogniser heard (its full text) is from what the voice was asked to say, both normalised.
    Whisper invents a lone "you" in silence (mlx-examples#1402); that is not counted as extra."""
    import difflib
    r, h = _norm_words(ref), _norm_words(hyp_text)
    while h and h[-1] == "you" and (not r or r[-1] != "you"):      # the invented "you" sits in the closing silence
        h = h[:-1]
    sm = difflib.SequenceMatcher(a=r, b=h, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    missing, extra = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            missing += r[i1:i2]
        if tag in ("replace", "insert"):
            extra += [w for w in h[j1:j2] if w != "you"]
    # the end is heard when the last letters and digits match, give or take a spelling ("20 m" / "20M", "metres" /
    # "meters", "dashcam" / "dash cam"): a word-level test flagged 5 of 76 clean scenes
    rc, hc = "".join(r), "".join(h)
    tail_ok = bool(r) and (rc[-6:] in hc[-24:] or difflib.SequenceMatcher(a=rc[-10:], b=hc[-14:], autojunk=False).ratio() >= 0.6)
    return {"wer": round(1 - matched / len(r), 3) if r else 0.0, "missing": missing[:12], "extra": extra[:12], "end_heard": tail_ok,
            "rule": AUDIO_CHECK_RULE,
            "char_sim": round(difflib.SequenceMatcher(a="".join(r), b="".join(h), autojunk=False).ratio(), 3) if r else 1.0}


def _wav_read(p: Path) -> bytes:
    import wave
    with wave.open(str(p), "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (VOICE_SR, 2, 1):
            raise SystemExit(f"{p}: expected {VOICE_SR} Hz 16-bit mono")
        return w.readframes(w.getnframes())


def _wav_write(p: Path, frames: bytes) -> None:
    import wave
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(VOICE_SR)
        w.writeframes(frames)


def _silence(s: float) -> bytes:
    return b"\x00\x00" * int(round(s * VOICE_SR))


def _run_worker(mode: str, job: dict, work: Path) -> dict:
    import subprocess
    py = voice_python()
    if not py.exists():
        raise SystemExit(f"no voice venv at {voice_home()}: run `litrev.py voice-setup` first (about 1 GB with the models)")
    jf, rf = work / f"job-{mode}.json", work / f"result-{mode}.json"
    jf.write_text(json.dumps(job))
    rf.unlink(missing_ok=True)
    p = subprocess.run([str(py), str(Path(__file__).with_name("voice_worker.py")), mode, str(jf), str(rf)],
                       capture_output=True, text=True, cwd=str(work))
    if p.returncode != 0 or not rf.exists():
        raise SystemExit(f"voice worker ({mode}) failed: {p.stderr[-1500:]}")
    if "truncat" in p.stderr.lower():       # kokoro keeps its truncation warning on stderr (kokoro#367)
        print(f"WARNING: the voice engine truncated a sentence: {p.stderr[-600:]}", file=sys.stderr)
    return json.loads(rf.read_text())


def _loudness(p: Path) -> float | None:
    import shutil, subprocess
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    r = subprocess.run([ff, "-nostats", "-hide_banner", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else None


def load_audio(rv: Review) -> dict:
    try:
        d = json.loads((_pres_dir(rv) / "audio.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _sentence_key(voice: str, text: str) -> str:
    return hashlib.sha1(f"{voice}\x00{VOICE_LANG}\x00{text}".encode()).hexdigest()[:16]


def _synth(rv: Review, sents: list[tuple[str, str]], voice: str, work: Path, force: bool) -> dict:
    """{key: {dur, tokens, out}} for (key, spoken text) pairs; only the sentences not cached are spoken."""
    idx_f = work / "index.json"
    try:
        idx = json.loads(idx_f.read_text())
    except (OSError, ValueError):
        idx = {}
    todo = [(k, t) for k, t in dict(sents).items() if force or k not in idx or not (work / f"{k}.wav").exists()]
    if todo:
        res = _run_worker("tts", {"voice": voice, "lang": VOICE_LANG, "speed": 1.0,
                                  "items": [{"id": k, "text": t, "out": str(work / f"{k}.wav")} for k, t in todo]}, work)
        for it in res["items"]:
            if it.get("error"):
                raise SystemExit(f"voice engine gave no audio for: {dict(todo)[it['id']]!r}")
            idx[it["id"]] = {k: it[k] for k in ("dur", "tokens", "trimmed_s", "chunks")}
        idx["_engine"] = res.get("engine")
        idx_f.write_text(json.dumps(idx))
    return idx


def audio_build(rv: Review, chapter: str | None = None, voice: str | None = None, sample: bool = False,
                asr: bool = True, force: bool = False) -> dict:
    """Speak the merged script: one WAV per scene (cache/presentation/audio/), measured times in
    data/presentation/audio.json, and a check of each scene against speech recognition. `sample` speaks only three
    sentences (a technical term, a number, a transition) to rendered/presentation/sample.wav, for a listen first."""
    pres = load_presentation(rv)
    plan, sc, lex = pres.get("plan"), pres.get("script") or {}, pres.get("lexicon") or {}
    if not plan or not sc:
        raise SystemExit("no merged script: run merge-presentation-plan and merge-script first")
    prev = load_audio(rv)
    voice = voice or prev.get("voice") or VOICE_DEFAULT
    work = rv.cache_dir / "presentation" / "audio"
    work.mkdir(parents=True, exist_ok=True)
    chs = [c for c in plan["chapters"] if c["id"] in sc and (not chapter or c["id"] == chapter)]
    if chapter and not chs:
        raise SystemExit(f"{chapter} has no merged script")
    if sample:
        flat = [(c["id"], s, t) for c in chs for s in sc[c["id"]]["scenes"] for t in s["narration"]]
        pick = [next((x for x in flat if any(re.search(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", x[2]) for k in lex)), None),
                next((x for x in flat if PRES_NUM.search(x[2]) and re.search(r"\d\.\d", x[2])), None),
                next((x for c in chs for x in [(c["id"], sc[c["id"]]["scenes"][-1], sc[c["id"]]["scenes"][-1]["narration"][-1])]), None)]
        pick = list(dict.fromkeys(p[2] for p in pick if p))
        pairs = [(_sentence_key(voice, _spoken(t, lex)), _spoken(t, lex)) for t in pick]
        idx = _synth(rv, pairs, voice, work, force)
        frames = b"".join(_wav_read(work / f"{k}.wav") + _silence(0.6) for k, _ in pairs)
        out = rv.path / "rendered" / "presentation"
        out.mkdir(parents=True, exist_ok=True)
        _wav_write(out / "sample.wav", frames)
        rep = {"sample": str(out / "sample.wav"), "voice": voice, "sentences": pick, "spoken": [t for _, t in pairs]}
        if asr:
            _wav_write(work / "sample.wav", frames)
            heard = _run_worker("asr", {"model": ASR_MODEL, "items": [{"id": "sample", "wav": str(work / "sample.wav")}]}, work)["items"][0]
            rep["heard"], rep["check"] = heard["text"], _asr_compare(" ".join(t for _, t in pairs), heard["text"])
        return rep
    pairs, plan_s = [], {}
    for c in chs:
        for s in sc[c["id"]]["scenes"]:
            spoken = [_spoken(t, lex) for t in s["narration"]]
            plan_s[s["id"]] = (c["id"], s, spoken, [_sentence_key(voice, t) for t in spoken])
            pairs += list(zip(plan_s[s["id"]][3], spoken))
    idx = _synth(rv, pairs, voice, work, force)
    scenes = dict(prev.get("scenes") or {}) if prev.get("voice") == voice else {}
    rebuilt, asr_items = [], []
    for sid, (ch, s, spoken, keys) in plan_s.items():
        frames, starts, words, t = b"", [], [], 0.0
        for n, k in enumerate(keys):
            if n:
                frames += _silence(AUDIO_SENT_GAP_S)
                t += AUDIO_SENT_GAP_S
            starts.append(round(t, 3))
            words += [{"text": w["text"], "start": round(t + w["start"], 3), "end": round(t + w["end"], 3), "sentence": n}
                      for w in idx[k]["tokens"] if any(ch_.isalnum() for ch_ in w["text"])]
            frames += _wav_read(work / f"{k}.wav")
            t += idx[k]["dur"]
        frames += _silence(s.get("pause_after_s") or 0)
        speech_end, t = t, t + (s.get("pause_after_s") or 0)
        wav = work / f"{sid}.wav"
        _wav_write(wav, frames)
        old = scenes.get(sid) or {}
        # silences from the voice engine's own word times: whisper's word times stretch around merged tokens
        # ("QPP0.4669" gave a false 1.68 s gap where Kokoro's longest was 0.7 s)
        gaps = [round(b["start"] - a["end"], 2) for a, b in zip(words, words[1:]) if b["start"] - a["end"] > AUDIO_GAP_FLAG_S]
        scenes[sid] = {"chapter": ch, "wav": str(wav.relative_to(rv.path)), "dur": round(t, 3), "speech_end": round(speech_end, 3),
                       "sentence_starts": starts, "words": words, "keys": keys, "gaps": gaps,
                       "asr": old.get("asr") if old.get("keys") == keys else None, "loudness_lufs": _loudness(wav)}
        if asr and scenes[sid]["asr"] is None:
            asr_items.append({"id": sid, "wav": str(wav)})
        rebuilt.append(sid)
    if asr_items:
        for it in _run_worker("asr", {"model": ASR_MODEL, "items": asr_items}, work)["items"]:
            ch, s, spoken, keys = plan_s[it["id"]]
            cmp_ = _asr_compare(" ".join(spoken), it["text"])
            cmp_["char_sim"] = max(cmp_["char_sim"], _asr_compare(" ".join(s["narration"]), it["text"])["char_sim"])
            scenes[it["id"]]["asr"] = dict(cmp_, heard=it["text"])
    for sid, (ch, s, spoken, keys) in plan_s.items():   # a check stored before a rule changed: recompute it from the text heard
        a_ = scenes[sid].get("asr")
        if a_ and a_.get("rule") != AUDIO_CHECK_RULE and "heard" in a_:
            cmp_ = _asr_compare(" ".join(spoken), a_["heard"])
            cmp_["char_sim"] = max(cmp_["char_sim"], _asr_compare(" ".join(s["narration"]), a_["heard"])["char_sim"])
            scenes[sid]["asr"] = dict(a_, **cmp_)
    keep = {sid for c in plan["chapters"] if c["id"] in sc for s in sc[c["id"]]["scenes"] for sid in [s["id"]]}
    scenes = {k: v for k, v in scenes.items() if k in keep}
    loud = sorted(v["loudness_lufs"] for v in scenes.values() if v.get("loudness_lufs") is not None)
    med = loud[len(loud) // 2] if loud else None
    flags = []
    for sid, v in scenes.items():
        a = v.get("asr") or {}
        if (a and (a.get("char_sim", 1.0) < AUDIO_CHAR_FLAG or not a.get("end_heard", True))) or v.get("gaps"):
            flags.append({"scene": sid, "char_sim": a.get("char_sim"), "wer": a.get("wer"), "missing": a.get("missing"),
                          "extra": a.get("extra"), "end_heard": a.get("end_heard"), "gaps": v.get("gaps"), "heard": a.get("heard")})
        if med is not None and v.get("loudness_lufs") is not None and abs(v["loudness_lufs"] - med) > AUDIO_LOUD_FLAG_LU:
            flags.append({"scene": sid, "loudness_lufs": v["loudness_lufs"], "median_lufs": med})
    chapters = {}
    for c in plan["chapters"]:
        ds = [v["dur"] for sid, v in scenes.items() if v["chapter"] == c["id"]]
        if c["id"] in sc and len(ds) == len(sc[c["id"]]["scenes"]):
            planned = c["end_s"] - c["start_s"]
            chapters[c["id"]] = {"dur": round(sum(ds), 1), "planned": planned, "drift": round((sum(ds) - planned) / planned, 3)}
    audio = {"voice": voice, "engine": idx.get("_engine"), "asr_model": ASR_MODEL if asr else None, "built_at": now_iso(),
             "scenes": scenes, "chapters": chapters, "total_s": round(sum(v["dur"] for v in chapters.values()), 1),
             "target_s": plan.get("target_s"), "flags": flags}
    (_pres_dir(rv) / "audio.json").write_text(json.dumps(audio, indent=1, ensure_ascii=False))
    drift = {k: v["drift"] for k, v in chapters.items() if abs(v["drift"]) > AUDIO_DRIFT_FLAG}
    listen = _narration_file(rv, plan, sc, scenes)
    return {"scenes_built": len(rebuilt), "sentences_spoken": sum(1 for _ in pairs), "voice": voice,
            "total_s": audio["total_s"], "target_s": audio["target_s"], "chapters_off_plan": drift, "flags": flags,
            "listen": listen}


def _narration_file(rv: Review, plan: dict, sc: dict, scenes: dict) -> str | None:
    """The whole narration in plan order as rendered/presentation/narration.m4a, for a listen before the video
    (gitignored; needs ffmpeg). Only when every chapter has its audio."""
    import shutil, subprocess
    order = [s["id"] for c in plan["chapters"] for s in (sc.get(c["id"]) or {}).get("scenes", [])]
    ff = shutil.which("ffmpeg")
    if not ff or not order or any(sid not in scenes for sid in order):
        return None
    wav = rv.cache_dir / "presentation" / "narration.wav"
    _wav_write(wav, b"".join(_wav_read(rv.path / scenes[sid]["wav"]) for sid in order))
    out = rv.path / "rendered" / "presentation" / "narration.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "96k", str(out)],
                       capture_output=True, text=True)
    return str(out) if p.returncode == 0 else None


def _audio_status_rows(rv: Review, row) -> None:
    pres = load_presentation(rv)
    sc = pres.get("script") or {}
    if not sc or not pres.get("plan"):
        return
    a, lex = load_audio(rv), pres.get("lexicon") or {}
    voice = a.get("voice") or VOICE_DEFAULT
    want = {s["id"]: [_sentence_key(voice, _spoken(t, lex)) for t in s["narration"]] for v in sc.values() for s in v["scenes"]}
    have = a.get("scenes") or {}
    miss = [sid for sid in want if sid not in have]
    old = [sid for sid in want if sid in have and have[sid].get("keys") != want[sid]]
    detail = "; ".join(x for x in (f"no audio: {_ids(miss)}" if miss else "", f"text or lexicon changed: {_ids(old)}" if old else "",
                                   f"{len(a.get('flags') or [])} scene(s) flagged by the voice check" if a.get("flags") else "") if x)
    off = {k: v["drift"] for k, v in (a.get("chapters") or {}).items() if abs(v.get("drift") or 0) > AUDIO_DRIFT_FLAG}
    if off and not (miss or old):
        detail = "; ".join(x for x in (detail, f"{a.get('total_s')} s of {a.get('target_s')} s; chapters off their planned time: "
                                       + ", ".join(f"{k} {v:+.0%}" for k, v in sorted(off.items()))) if x)
    row(8, "audio", "todo" if miss and not have else ("stale" if miss or old else ("warn" if a.get("flags") or off else "done")),
        detail or f"{len(have)} scenes, {a.get('total_s')} s of {a.get('target_s')} s, voice {voice}",
        "audio [--chapter CH##] (audio --sample first)" if miss or old else
        ("listen to the flagged scenes; fix the lexicon or the script" if a.get("flags") else
         "shorten those chapters (script-input -> writer with the merge's word target -> merge-script -> audio) or re-plan the times" if off else ""))


# --------------------------------------------------------------------------
# step 8: the video (scene page data here; stills, ffmpeg and the sync check in scripts/video_build.py)
# --------------------------------------------------------------------------

def _short_title(p: dict) -> str:
    """The hub's short title: the name before a colon, else the first 50 characters."""
    t = str(p.get("title") or p.get("id") or "")
    i = t.find(":")
    return t[:i] if 1 < i < 42 else (t if len(t) <= 52 else t[:50] + "...")


def _trim(s, n: int = 220) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _lead(s, n: int) -> str:
    """Whole sentences up to n characters (a frame never shows a sentence cut in the middle); a first sentence longer
    than n is cut at a word."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    out = ""
    for sent in re.split(r"(?<=[.!?])(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\bal\.)(?<!\bcf\.)(?<!\bFig\.)(?<!\bEq\.)(?<!\bSec\.)\s+(?=[A-Z0-9(])", s):
        if len(out) + len(sent) + (1 if out else 0) > n:
            break
        out = f"{out} {sent}".strip()
    return out or _trim(s, n)


_LOC_REF = re.compile(r"\b(Table|Tab\.|Figure|Fig\.)\s*([A-Z]?\d+[a-z]?)")
# a reported spread, as step 5 keeps it in a row's caveats ("mean +/- 0.65 std over 3 runs") or its quote ("57.60 \pm 0.65")
_SPREAD = re.compile(r"(?:\+/-|\u00b1|\\pm(?:\s*\\?[a-z]*\s*)?\{?)\s*(\d+(?:\.\d+)?)")
_RUNS = re.compile(r"\b(?:over|across|of|from)\s+(\d+|three|five|ten)\s+(?:independent\s+)?(?:runs|seeds|trials)\b", re.I)


def result_spread(r: dict) -> dict | None:
    """The spread a result row reports ({"pm": "0.50", "runs": 3}), from its caveats, then its quote; None when the
    row is a single value. CURE-Med's ablation reports mean +/- std over 3 runs; its MedQA row is one value."""
    for txt in (r.get("caveats"), r.get("quote")):
        m = _SPREAD.search(str(txt or ""))
        if m:
            n = _RUNS.search(str(r.get("caveats") or "") + " " + str(r.get("protocol") or ""))
            runs = {"three": 3, "five": 5, "ten": 10}.get(n.group(1).lower(), None) if n and not n.group(1).isdigit() else int(n.group(1)) if n else None
            return {"pm": m.group(1), **({"runs": runs} if runs else {})}
    return None


def _source_papers(rv: Review, ref: str, ctx: _PresCtx) -> list[str]:
    """The papers a reviewer's text (a takeaway, a shared problem, a report item) rests on, as short references."""
    T = rv.tables
    x = _pres_record(rv, ref, ctx, full=False)
    ids = [*(x.get("refs") or []), *(x.get("paper_ids") or []), *[c.get("paper_id") for c in (x.get("cells") or {}).values() if isinstance(c, dict)]]
    out = []
    for r in ids:
        pid = r if r in T["papers"] else (T["results"].get(r) or {}).get("paper_id") if r in T["results"] else \
            (T["clusters"].get(r) or {}).get("winner_paper_id") if r in T["clusters"] else None
        if pid in T["papers"]:
            out.append(f"{ctx.name(pid)} {T['papers'][pid].get('year') or ''}".strip())
    return list(dict.fromkeys(out))


def _video_source(rv: Review, ref: str, ctx: _PresCtx, scene_papers: set | None = None) -> str:
    """A reference a viewer recognises: 'Paper short title year · Table N'; a reviewer's text names the papers it rests
    on. Never a record id or a report item number (an outside review: 'review report: condition 4' and 'and 1 more' do
    little for the viewer)."""
    T, M = rv.tables, rv.meta
    if ref in T["papers"]:
        p = T["papers"][ref]
        return f"{ctx.name(ref)} {p.get('year') or ''}".strip()
    if ref in T["results"]:
        r = T["results"][ref]
        p = T["papers"].get(r.get("paper_id") or "") or {}
        loc = _LOC_REF.search(f"{r.get('location') or ''} {r.get('protocol') or ''}")
        tgt = T["benchmarks"].get(r.get("benchmark_id") or "") or T["datasets"].get(r.get("dataset_id") or "") or {}
        where = f"{'Table' if loc.group(1).startswith('Tab') else 'Figure'} {loc.group(2)}" if loc else tgt.get("name") or ""
        return " · ".join(x for x in (f"{ctx.name(r.get('paper_id'))} {p.get('year') or ''}".strip(), where) if x)
    if re.fullmatch(r"K\d{3,}", ref):
        k = next((k for k in M.get("kpis") or [] if k["id"] == ref), None)
        return f"comparison: {_kpi_short(rv, k)}" if k else ""
    if ref in T["clusters"]:
        wid = T["clusters"][ref].get("winner_paper_id") or ""
        w = T["papers"].get(wid)
        return f"{ctx.name(wid)} {w.get('year') or ''}".strip() if w else ""
    if re.fullmatch(r"OP\d+", ref):
        return "general knowledge of the field"
    if re.fullmatch(r"DEC\d{3,}", ref):
        return "review decision log"
    if re.fullmatch(r"(L|TK|U|CN|G|X|VD)\d+", ref):
        # name only papers the scene itself is about: a shared problem's first two cells named the pruning papers
        # under a Meerkat scene (video v3 frames, 2026-10-26)
        own = {f"{ctx.name(p)} {T['papers'][p].get('year') or ''}".strip() for p in scene_papers or set() if p in T["papers"]}
        ps = [x for x in _source_papers(rv, ref, ctx) if x in own]
        return ("reviewer's analysis of " + ", ".join(ps[:2])) if ps else "reviewer's analysis"
    for t in ("tasks", "datasets", "benchmarks", "metrics"):
        if ref in T[t]:
            return str(T[t][ref].get("name"))
    return ""


def _video_sources(rv: Review, refs: list, ctx: _PresCtx, kind: str | None = None, visual_refs: list | None = None) -> str:
    """At most three distinct references; the rest stay in the hub (no 'and N more'). The executive summary names no
    paper (the user, 2026-10-26), its sources line neither."""
    if kind in ("executive-summary", "verdict"):
        return "the review's synthesis of the included papers"
    T = rv.tables
    sp = set()
    for r in list(refs or []) + list(visual_refs or []):
        sp |= {r} if r in T["papers"] else {(T["results"].get(r) or {}).get("paper_id")} if r in T["results"] else \
            {(T["clusters"].get(r) or {}).get("winner_paper_id")} if r in T["clusters"] else set()
    out = list(dict.fromkeys(x for x in (_video_source(rv, r, ctx, sp) for r in refs or []) if x))
    out = [x for x in out if not any(y != x and y.startswith(x + " · ") for y in out)]   # "Paper 2024" beside "Paper 2024 · Table 3"
    return " · ".join(out[:3])


_ID_PAREN = re.compile(rf"\s*\((?:{_VIEWER_IDS}(?:\s*(?:,|;|and|&)\s*)?)+\)")
_ID_NICK = re.compile(r"\bC\d{3,}\s*\(([^()]{3,60})\)")


def _viewer_text(rv: Review, s):
    """A text shown to a viewer, without record ids: '(C007)' and '(P6493)' go, 'C005 (weights removed once)' keeps its
    nickname, a bare family, paper or KPI id becomes its name. The first 30-minute video showed C005, C007, C008 and
    C009 in its takeaways, conditions and gaps (llm-to-domain-slm, 2026-10-26)."""
    if not isinstance(s, str) or not s:
        return s
    T = rv.tables
    s = _ID_NICK.sub(lambda m: m.group(1), s)
    s = _ID_PAREN.sub("", s)

    def name(m):
        i = m.group(0)
        if i in T["clusters"]:
            return f"the {T['clusters'][i].get('name')} family"
        if i in T["papers"]:
            return _short_title(T["papers"][i])
        k = next((k for k in rv.meta.get("kpis") or [] if k["id"] == i), None)
        return _kpi_short(rv, k) if k else i
    s = re.sub(r"\b(?:C\d{3,}|P\d{3,}|K\d{3,})\b", name, s)
    # "board" blurs the review's comparison and a public leaderboard (the narration avoids it; report texts on screen
    # used it: "The two boards for LLaMA-2-7B ...")
    s = re.sub(r"\b(?:comparison|KPI|ranking) (board)(s?)\b", lambda m: m.group(0).split()[0] + m.group(2), s)
    s = re.sub(r"\b[Bb]oard(s?)\b", lambda m: ("Comparison" if m.group(0)[0] == "B" else "comparison") + m.group(1), s)
    return re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;:])", r"\1", s)).strip()


def video_data(rv: Review) -> tuple[dict, list[dict]]:
    """The scene page's data and the timeline's scenes, in plan order. Every number on screen comes from the review."""
    import video_build as vb
    pres, audio = load_presentation(rv), load_audio(rv)
    plan, sc = pres.get("plan"), pres.get("script") or {}
    if not plan or any(c["id"] not in sc for c in plan["chapters"]):
        raise SystemExit("the video needs a merged script for every chapter (merge-script)")
    A = audio.get("scenes") or {}
    order = [(ci, s) for ci, c in enumerate(plan["chapters"]) for s in sc[c["id"]]["scenes"]]
    miss = [s["id"] for _, s in order if s["id"] not in A]
    if miss:
        raise SystemExit(f"no audio for {_ids(miss)}: run `audio` first")
    ctx, T, M = _PresCtx(rv), rv.tables, rv.meta
    papers = T["papers"]
    clusters = sorted(T["clusters"].values(), key=lambda c: c["id"])
    rts = ctx.result_tables()
    figs = embed_explainer_figures(rv)
    obj: dict = {}

    def cluster_obj(cid: str) -> dict:
        c, x = T["clusters"][cid], ctx.X.get(cid) or {}
        val = lambda e: e.get("value") if isinstance(e, dict) else e
        rt = rts.get(x.get("id")) if x else None
        table = None
        if rt and rt.get("rows"):
            cols = [" ".join(str(v) for v in (col.get("dataset"), col.get("metric"), col.get("variant")) if v) for col in rt["columns"]]
            R = T["results"]

            def cell(cl):       # "54.35 ± 0.50" when the paper reports a spread for that row
                if cl is None:
                    return None
                sp = result_spread(R.get(cl.get("result_id") or "") or {})
                return f"{cl.get('value')} \u00b1 {sp['pm']}" if sp and cl.get("value") is not None else cl.get("value")
            table = {"caption": rt.get("why"), "columns": cols, "col_ids": [f"col{i}" for i in range(1, len(cols) + 1)],
                     "rows": [{"label": r.get("label"), "own": r.get("own"),
                               "cells": [cell(cl) for cl in r.get("cells") or []],
                               "best": [bool(cl and cl.get("best")) for cl in r.get("cells") or []]} for r in rt["rows"]]}
        w = papers.get(c.get("winner_paper_id") or "") or {}
        return {"name": c.get("name"), "n_members": len(c.get("paper_ids") or []), "winner": ctx.name(w["id"]) if w else None,
                "diagram": x.get("diagram"), "table": table,
                "facets": {**{f: _lead(val(x.get(f)), 160) for f in ("problem", "key_idea", "how_it_works")},
                           **{f: _lead(val(e), 110) for f, e in (x.get("facets") or {}).items()}}}

    def kpi_obj(kid: str) -> dict:
        k = next(k for k in M.get("kpis") or [] if k["id"] == kid)
        hib = (T["metrics"].get(k.get("metric_id") or "") or {}).get("higher_is_better")
        in_c = {pid: c["id"] for c in clusters for pid in c.get("paper_ids") or []}
        def name(e):        # the method's own name, as the narration says it: "QPP-GenRE (n=10), LLaMA-7B ..." -> QPP-GenRE
            mv = re.sub(r"\s*\((ours|full)\)", "", str(e.get("method_variant") or ""), flags=re.I)
            n = re.split(r"\s*[(,;:]", mv)[0].strip()
            # result tables often call the paper's own method "Ours", or a row label is a whole sentence ("D-Pruner
            # without iterative blocking on LLaMA2-7B"): then the paper's display name names it
            if not 1 < len(n) <= 32 or re.fullmatch(r"(ours|our method|proposed|full|baseline|method|model)\*?", n, re.I):
                return ctx.name(e["paper_id"])
            return n
        board = ctx.board(kid)
        in_pct = _board_in_percent(board)          # one scale per board, as the writers' packets give it
        rows = [{"paper_id": e["paper_id"], "label": name(e), "rank": e["rank"],
                 "sub": (f"{ctx.name(e['paper_id'])} ({papers[e['paper_id']].get('year')})"
                         if name(e) != ctx.name(e["paper_id"]) else f"({papers[e['paper_id']].get('year')})"),
                 "value": e.get("raw_value") if _shown_value(e, in_pct) is None else _shown_value(e, in_pct),
                 "num": e.get("value"), "cluster": in_c.get(e["paper_id"])} for e in board]
        return {"label": _kpi_short(rv, k), "rows": rows, "units": "%" if in_pct else "",
                "note": f"{len(rows)} papers, best row per paper",
                "direction": {True: "higher is better", False: "lower is better"}.get(hib, "")}

    def paper_obj(pid: str) -> dict:
        p = papers[pid]
        x = next((x for x in T["explainers"].values() if x.get("paper_id") == pid), None)
        f = figs.get(x["id"]) if x else None
        return {"short": ctx.name(pid), "figure": (f or {}).get("src"), "caption": _trim((f or {}).get("caption"), 260),
                "figure_label": ((x or {}).get("figure") or {}).get("label"), "year": p.get("year"),
                "cluster": next((c["id"] for c in clusters if pid in (c.get("paper_ids") or [])), None),
                "dims": {k: _lead(n["value"], 260) for k, n in _method_notes(p).items()       # the scene cuts them to fit 1-3 papers
                         if k in PRES_DIMENSIONS and n.get("value")}}

    scenes_d, tl = [], []
    for ci, s in order:
        v = s.get("visual") or {}
        kind, refs = v.get("kind") or "claim", [r for r in v.get("refs") or []]
        for r in refs:
            if r not in obj:
                obj[r] = cluster_obj(r) if r in T["clusters"] else kpi_obj(r) if re.fullmatch(r"K\d{3,}", r) else paper_obj(r) if r in papers else {}
        if kind == "taxonomy":
            for c in refs or [c["id"] for c in clusters]:
                obj.setdefault(c, cluster_obj(c))
        src = _video_sources(rv, s.get("sources") or [], ctx, kind, refs)
        a = A[s["id"]]
        lines = [_viewer_text(rv, l.strip()) for l in str(s.get("on_screen") or "").split("\n") if l.strip()]
        states = vb.states_of(v)
        if kind in ("claim", "example") and v.get("reveal"):      # the page dims unrevealed lines only then
            states = vb.show_unnamed(states, len(lines))
        if kind in PRES_SUMMARY_KINDS:      # a summary table is read whole: a reveal highlights the rows its sentence names
            states = [dict(st, shown=[r["id"] for r in ctx.summaries()[kind]["rows"]]) for st in states]
        if kind == "standing" and not v.get("reveal"):        # no reveal: every comparison from the start
            states = [dict(st, shown=[k["id"] for k in M.get("kpis") or []]) for st in states]
        scenes_d.append({"id": s["id"], "ch": ci, "kind": kind, "refs": refs, "states": states,
                         "reveals": bool(v.get("reveal")), "lines": lines,
                         "claim": s.get("claim_type"), "sources": src,
                         "illustrative": s.get("claim_type") == "illustrative" or kind == "example"})
        tl.append({"id": s["id"], "dur": a["dur"], "sentence_starts": a["sentence_starts"], "reveal": v.get("reveal") or [],
                   "transition": s.get("transition"), "wav": str(rv.path / a["wav"]), "narration": s["narration"], "words": a["words"]})
    lim = M.get("limitations") or {}
    fam_ids = [c["id"] for c in clusters]
    data = {"title": str(M.get("seed") or M.get("slug") or "").strip(),
            "subtitle": f"A {plan.get('minutes')}-minute narrated literature review", "chapters": [
                {"id": c["id"], "title": c.get("title"), "question": c.get("question")} for c in plan["chapters"]],
            "families": [{"id": c["id"], "name": c.get("name")} for c in clusters], "scenes": scenes_d, "obj": obj,
            "scope": {k: (M.get("scope") or {}).get(k) or [] for k in ("in", "out", "protocol")},
            "flow": _evidence_base(rv)["flow"], "summaries": ctx.summaries(),
            "takeaways": [{"id": f"TK{i}", "text": _lead(_viewer_text(rv, t.get("text")), 170)} for i, t in enumerate((M.get("synthesis") or {}).get("takeaways") or [], 1)],
            "open": [{"id": f"OP{i}", "title": o.get("title"), "text": _lead(_viewer_text(rv, o.get("text")), 150)} for i, o in enumerate(lim.get("open_problems") or [], 1)],
            "matrix": {"families": fam_ids, "rows": [{"id": p.get("id"), "title": _viewer_text(rv, p.get("title")),
                                                      "cells": {cid: f"{e.get('state')}:{e.get('basis')}" for cid, e in (p.get("cells") or {}).items()
                                                                if e.get("state") in ("affected", "addresses")}}
                                                     for p in lim.get("problems") or []]},
            "standing": {c["id"]: _pres_standing(rv, ctx, c["id"]) for c in clusters} if M.get("kpis") else {},
            "familyTable": [{"id": c["id"], "name": c.get("name"), **{f: _lead(((ctx.X.get(c["id"]) or {}).get("facets") or {}).get(f, {}).get("value"), 110)
                                                                          for f in FACETS}} for c in clusters]}
    data.update(_video_section_data(rv, ctx))
    return tex_display(data), tl          # formulas as text, as the hub shows them: "FG$^2$" -> "FG²"


def _video_section_data(rv: Review, ctx: _PresCtx) -> dict:
    """What the ten sections' data scenes draw: the review report's parts and the computed section tables. A frame
    holds less than the hub: texts are cut to whole sentences (_lead) at lengths the layout test fits at their most
    items (6 summary parts, 8 gaps, 5 experiments); the narration says the rest and the hub's Report tab has it all."""
    r, papers = _pres_report(rv), rv.tables["papers"]
    short = lambda pid: ctx.name(pid) if pid in papers else pid
    rm = r.get("roadmap") or {}
    req = r.get("requirements") or {}
    vt = lambda x: _viewer_text(rv, x)
    return {"exec": [{"id": k, "label": PRES_EXEC_LABELS[k], "text": _lead(vt(((r.get("executive_summary") or {}).get(k) or {}).get("text")), 200)}
                     for k in _exec_parts(r)],
            # the top-down verdict: one row per goal, coloured by the family that serves it better
            "verdict": [{"id": x["id"], "goal": vt(x.get("goal")), "choice": vt(x.get("choice")), "families": x.get("cluster_ids") or [],
                         "principle": _lead(vt(x.get("principle")), 150), "condition": _lead(vt(x.get("condition")), 80),
                         "support": REPORT_SUPPORT.get(x.get("support"), x.get("support"))} for x in r.get("verdict") or []],
            "req": [{"id": k, "label": lab, "text": (_lead(vt(req.get(k)), 160) if k != "research_questions" else None),
                     "list": [_lead(vt(q), 110) for q in req.get(k) or []] if k == "research_questions" else None}
                    for k, lab in PRES_REQ_KEYS.items()],
            "search": _pres_search(rv), "prov": _pres_provenance(rv, ctx), "weak": _pres_weak_points(rv, ctx),
            "feas": _pres_feasibility(rv, ctx), "dimLabels": PRES_DIMENSIONS, "gapTypes": REPORT_GAP_TYPES,
            **{k: [dict(x, text=_lead(vt(x.get("text")), 150)) for x in r.get(k) or []] for k in REPORT_LISTS},
            # experiment cards: the question, what is measured, the decision (an outside review: a dense table where the
            # audience needs an action plan); the stop criteria and needs stay in the hub
            "roadmap": {"baseline": dict(rm["baseline"], short=short(rm["baseline"].get("paper_id")),
                                         reason=_lead(vt(rm["baseline"].get("reason")), 110)) if rm.get("baseline") else None,
                        "shortlist": [dict(x, short=short(x.get("paper_id"))) for x in rm.get("shortlist") or []],
                        "experiments": [dict(x, **{k: _lead(vt(x.get(k)), 120) for k in ("experiment", "question", "decision")})
                                        for x in rm.get("experiments") or []]}}


# A picture change leads its sentence's first word by 0 to 0.5 s (measured: 0.22-0.29 s, Kokoro's lead-in silence).
# The recogniser checks the finished file end to end: median offset within 0.5 s, drift from the first to the last
# third within 0.15 s (measured +0.21 / +0.24 / +0.27 s over 12 minutes, recogniser noise); its single words can be
# 1 s off near numbers and acronyms, so they are listed, not failed.
VIDEO_SYNC_OK_S = 0.5


def _video_sync(rv: Review, mp4: Path, segs: list[dict], tl: list[dict], fps: int) -> dict:
    """End to end, on the finished file: speech recognition on the MP4's own audio track gives the time each sentence
    that starts a scene or a reveal is first heard; compare it with the frame where the picture changes."""
    lex = load_presentation(rv).get("lexicon") or {}
    heard = _run_worker("asr", {"model": ASR_MODEL, "items": [{"id": "video", "wav": str(mp4)}]}, rv.cache_dir / "presentation" / "audio")["items"][0]
    words = [(tok, w["start"]) for w in heard["words"] for tok in _norm_words(w["word"])]     # "one" -> "1", as the script
    toks = [w for w, _ in words]
    same = lambda a, b: bool(a and b) and (a == b or (len(a) > 2 and len(b) > 2 and (a.startswith(b) or b.startswith(a))))
    events = []
    for sg in segs:
        if sg["kind"] != "still":
            continue
        sc = tl[sg["scene"]]
        ats = [0] + sorted({r["at"] for r in sc["reveal"] if isinstance(r.get("at"), int) and r["at"] > 0})
        at = ats[sg["state"]] if sg["state"] < len(ats) else None
        if at is None:
            continue
        key = _norm_words(_spoken(sc["narration"][at], lex))[:3]       # the first three words, not one: "and" is everywhere
        if not key:
            continue
        t, best = sg["start_f"] / fps, None
        for j, (_, st) in enumerate(words):
            if not -1.5 <= st - t <= 2.5:
                continue
            score = sum(1 for n, k in enumerate(key) if j + n < len(toks) and same(toks[j + n], k))
            if score >= min(2, len(key)) and (best is None or (score, -abs(st - t)) > (best[0], -abs(best[1] - t))):
                best = (score, st)
        events.append({"t": round(t, 3), "scene": sc["id"], "sentence": at, "words": " ".join(key),
                       "heard_at": round(best[1], 3) if best else None})
    # 1. the voice engine's own clock, exact for every event: the scene WAVs are the audio track, sample for sample
    A, lead, starts_f, f = load_audio(rv).get("scenes") or {}, [], {}, 0
    for sc in tl:
        starts_f[sc["id"]], f = f, f + int(math.ceil(round(sc["dur"] * fps, 6)))
    for e in events:
        w = [x for x in (A.get(e["scene"]) or {}).get("words") or [] if x.get("sentence") == e["sentence"]]
        if w:
            e["voice_at"] = round(starts_f[e["scene"]] / fps + w[0]["start"], 3)
            lead.append(e["voice_at"] - e["t"])
    # 2. end to end: what the recogniser hears in the finished MP4 (its word times are noisier: +-1 s near numbers)
    d = [(e["t"], e["heard_at"] - e["t"]) for e in events if e["heard_at"] is not None]
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    third = max(1, len(d) // 3)
    drift = med([x for _, x in d[-third:]]) - med([x for _, x in d[:third]]) if len(d) >= 6 else None
    return {"events": len(events),
            "voice_clock": {"checked": len(lead), "min_lead_s": round(min(lead), 3) if lead else None,
                            "max_lead_s": round(max(lead), 3) if lead else None,
                            "in_window": sum(1 for x in lead if 0 <= x <= VIDEO_SYNC_OK_S), "window_s": [0, VIDEO_SYNC_OK_S]},
            "recogniser": {"matched": len(d), "median_s": round(med([x for _, x in d]), 3) if d else None,
                           "drift_first_to_last_third_s": round(drift, 3) if drift is not None else None,
                           "outliers_over_0.5s": [e for e in events if e["heard_at"] is not None and abs(e["heard_at"] - e["t"]) > VIDEO_SYNC_OK_S][:8],
                           "unmatched": [f"{e['scene']} sentence {e['sentence']} ({e['words']!r} at {e['t']} s)" for e in events if e["heard_at"] is None][:10]}}


def video_build(rv: Review, workers: int = 4, sync: bool = True) -> dict:
    """Render the narrated MP4 (rendered/presentation/<slug>.mp4, gitignored) with .srt/.vtt captions beside it, and
    write data/presentation/video.json: the timeline, the render times and the checks."""
    import video_build as vb
    data, tl = video_data(rv)
    work = rv.cache_dir / "presentation" / "video"
    if work.exists():
        import shutil
        shutil.rmtree(work)
    out = rv.path / "rendered" / "presentation" / f"{rv.meta.get('slug') or 'review'}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    rep = vb.build_video(data, tl, work, out, Path(__file__).with_name("video_template.html"), workers=workers)
    segs = rep.pop("segs")
    # the same stills as a PowerPoint deck beside the MP4 (the deck's states are the video's: deck_states = timeline cuts)
    rep["pptx"] = vb.build_pptx(data, tl, lambda si, k: work / "stills" / f"{si:03d}-{k:02d}.png", out.with_suffix(".pptx"), now_iso())
    a = load_audio(rv)
    rep["audio_built_at"] = a.get("built_at")
    rep["built_at"] = now_iso()
    if sync:
        rep["sync"] = _video_sync(rv, out, segs, tl, vb.FPS)
    rep["checks"] = {"frames_match": rep["probe"]["frames"] == rep["frames_expected"],
                     "audio_matches_video": abs(rep["probe"]["audio_s"] - rep["probe"]["video_s"]) <= 2 / vb.FPS + 0.03,
                     "picture_changes_found": not rep["picture_changes"]["missing"],
                     "no_layout_warnings": not rep["layout_warnings"]}
    if rep.get("sync"):
        sy = rep["sync"]
        vc, rc = sy["voice_clock"], sy["recogniser"]
        rep["checks"]["pictures_lead_speech"] = bool(vc["checked"]) and vc["in_window"] == vc["checked"]
        rep["checks"]["no_drift"] = rc["median_s"] is not None and abs(rc["median_s"]) <= VIDEO_SYNC_OK_S and \
            (rc["drift_first_to_last_third_s"] is None or abs(rc["drift_first_to_last_third_s"]) <= 0.15)
    (_pres_dir(rv) / "video.json").write_text(json.dumps(dict(rep, timeline=[{k: s[k] for k in s} for s in segs]), indent=1, ensure_ascii=False))
    return rep


def slides_build(rv: Review) -> dict:
    """The video's slides as a PowerPoint deck without the MP4 (rendered/presentation/<slug>.pptx, gitignored; `video`
    writes the same deck from its own stills): one slide per scene, its reveal states on clicks, the narration in the
    speaker notes. Needs Chrome and the recorded audio (the scene order and reveals), not ffmpeg."""
    import shutil
    import video_build as vb
    data, tl = video_data(rv)
    work = rv.cache_dir / "presentation" / "slides"
    if work.exists():
        shutil.rmtree(work)
    keys = [(si, k) for si, sc in enumerate(tl) for k, _ in vb.deck_states(sc)]
    warns = vb.render_stills(data, keys, work, Path(__file__).with_name("video_template.html"))
    out = rv.path / "rendered" / "presentation" / f"{rv.meta.get('slug') or 'review'}.pptx"
    return dict(vb.build_pptx(data, tl, lambda si, k: work / "stills" / f"{si:03d}-{k:02d}.png", out, now_iso()),
                layout_warnings=warns)


def _fmt_s(s) -> str:
    s = int(round(s or 0))
    return f"{s // 60:02d}:{s % 60:02d}"


def _audio_times(plan: dict, sc: dict, audio: dict) -> dict:
    """Measured start of every scene with audio, in plan order (the recorded audio sets the video's clock)."""
    t, out, A = 0.0, {}, (audio or {}).get("scenes") or {}
    for c in plan["chapters"]:
        for s in (sc.get(c["id"]) or {}).get("scenes", []):
            if s["id"] not in A:
                return out
            out[s["id"]] = round(t, 1)
            t += A[s["id"]]["dur"]
    return out


def _presentation_md(rv: Review, pres: dict, audio: dict | None = None) -> tuple[str, str]:
    plan, sc = pres["plan"], pres.get("script") or {}
    at, ach = _audio_times(plan, sc, audio), (audio or {}).get("chapters") or {}
    purl = pipeline_url()
    built = f"How this review was built: [Litrev Pipeline Atlas]({purl})\n\n" if purl else ""
    L = [f"# Presentation plan: {rv.meta.get('slug')}", "", built.strip(), "",
         f"{plan.get('minutes')} min · audience: {plan.get('audience') or '-'} · organizing axis: {plan.get('organizing_axis') or '-'}", ""]
    if plan.get("outcome"):
        L += [f"**Outcome.** {plan['outcome']}", ""]
    if plan.get("axis_reason"):
        L += [f"**Why this axis.** {plan['axis_reason']}", ""]
    L += ["**Headline answer.**", ""] + [f"- {h}" for h in plan.get("headline_answer") or []] + [""]
    ex = plan.get("recurring_example") or {}
    if ex.get("text"):
        L += [f"**Recurring example{' (illustrative)' if ex.get('illustrative') else ''}.** {ex['text']}", ""]
    old = int(plan.get("schema") or 1) < PRES_PLAN_SCHEMA
    L += [f"| Time | Duration | Chapter | Question | {'Functions' if old else 'Sections'} | Visual | Engagement | Takeaway |",
          "|---|---|---|---|---|---|---|---|"]
    cell = lambda x: str(x or "").replace("|", "\\|").replace("\n", " ")
    for c in plan["chapters"]:
        v, e = c.get("visual") or {}, c.get("engagement") or {}
        L.append(f"| {_fmt_s(c['start_s'])}–{_fmt_s(c['end_s'])} | {c['end_s'] - c['start_s']} s | {cell(c.get('title'))} | "
                 f"{cell(c.get('question'))} | {', '.join(map(str, c.get('functions' if old else 'sections') or []))} | {cell(v.get('kind'))} "
                 f"{cell(' '.join(v.get('refs') or []))} | {cell(e.get('kind'))} | {cell(c.get('takeaway'))} |")
    L.append("")
    for c in plan["chapters"]:
        ev = c.get("evidence") or {}
        L += [f"## {c['id']} {c.get('title')} ({_fmt_s(c['start_s'])}–{_fmt_s(c['end_s'])})", "",
              f"- **Viewer question:** {c.get('question') or '-'}",
              f"- **Prior knowledge:** {c.get('prior_knowledge') or '-'}",
              f"- **Main explanation:** {c.get('explanation') or '-'}",
              f"- **Evidence:** supporting {', '.join(ev.get('supporting') or []) or '-'}; challenging {', '.join(ev.get('challenging') or []) or '-'}",
              f"- **Comparability and limitations:** {c.get('comparability') or '-'}",
              f"- **Claims it states (one chapter each):** {', '.join(c.get('claims') or []) or '-'}",
              f"- **Families:** {', '.join(c.get('cluster_ids') or []) or '-'}"]
        if c.get("deep_dive"):
            L.append(f"- **Deep dive:** {c['deep_dive']['paper_id']}: {c['deep_dive'].get('reason')}")
        if c.get("summaries"):
            L.append(f"- **Summaries (one scene each):** {', '.join(c['summaries'])}")
        v = c.get("visual") or {}
        L += [f"- **Visual:** {v.get('kind')} {' '.join(v.get('refs') or [])}: {v.get('message') or ''}".rstrip(),
              f"- **Visual and narration sequence:** {c.get('visual_sequence') or '-'}",
              f"- **Worked example or analogy:** {c.get('example') or '-'}"]
        if c.get("engagement"):
            L.append(f"- **Engagement:** {c['engagement'].get('kind')}: {c['engagement'].get('prompt') or ''}")
        L += [f"- **Takeaway:** {c.get('takeaway') or '-'}", f"- **Transition:** {c.get('transition') or '-'}",
              f"- **Script:** {'%d scenes, %d words, about %s' % (len(sc[c['id']]['scenes']), sc[c['id']]['words'], _fmt_s(sc[c['id']]['est_s'])) if c['id'] in sc else 'not written yet'}", ""]
    if plan.get("folded_clusters"):
        L += ["## Families without their own chapter", ""] + [f"- {x['cluster_id']} → {x['into']}: {x['reason']}" for x in plan["folded_clusters"]] + [""]
    if plan.get("companion"):
        L += ["## In the searchable companion (the hub)", ""] + [f"- {x}" for x in plan["companion"]] + [""]
    if plan.get("missing_information"):
        L += ["## Missing information", ""] + [f"- {x}" for x in plan["missing_information"]] + [""]
    S = [f"# Narration script: {rv.meta.get('slug')}", "", built.strip(), ""]
    if audio and audio.get("total_s"):
        S += [f"Recorded narration: {_fmt_s(audio['total_s'])} for a {_fmt_s(audio.get('target_s'))} plan (voice "
              f"{audio.get('voice')}); scene times below are the recorded ones.", ""]
    else:
        S += ["Times are estimates from the spoken word count; the recorded audio (`audio`) sets the final times.", ""]
    for c in plan["chapters"]:
        rec = ach.get(c["id"])
        S += [f"## {c['id']} {c.get('title')} ({_fmt_s(c['start_s'])}–{_fmt_s(c['end_s'])}"
              + (f"; recorded {_fmt_s(rec['dur'])}, {rec['drift']:+.0%}" if rec else "") + ")", ""]
        if c["id"] not in sc:
            S += ["_Script not written yet._", ""]
            continue
        S += ["| Time | Narration | Visual | Sources | Claim |", "|---|---|---|---|---|"]
        for s in sc[c["id"]]["scenes"]:
            v = s.get("visual") or {}
            rev = "; ".join(f"after sentence {r['at'] + 1 if isinstance(r.get('at'), int) else '?'}: {', '.join(r['show'])}" for r in v.get("reveal") or [])
            S.append(f"| {_fmt_s(at.get(s['id'], s['start_s']))} | {cell(' '.join(s['narration']))} | {cell(v.get('kind'))} {cell(' '.join(v.get('refs') or []))}"
                     f"{(' (' + cell(rev) + ')') if rev else ''}{(' · ' + cell(s['on_screen'])) if s.get('on_screen') else ''} | "
                     f"{', '.join(s.get('sources') or [])} | {s.get('claim_type')}{' · ' + s['engagement']['kind'] if isinstance(s.get('engagement'), dict) else ''} |")
        S.append("")
    if pres.get("lexicon"):
        S += ["## Pronunciation", "", "| Term | Say |", "|---|---|"] + [f"| {cell(k)} | {cell(v)} |" for k, v in sorted(pres["lexicon"].items())] + [""]
    return "\n".join(L).replace("\n\n\n", "\n\n"), "\n".join(S).replace("\n\n\n", "\n\n")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def compute_set_ranks(rv: Review) -> dict:
    """Rank within the reviewed set per (dataset|benchmark, metric, split). Returns {result_id: (rank, denominator)}."""
    groups: dict[tuple, list] = {}
    for r in rv.tables["results"].values():
        if _num(r.get("value")) is None:
            continue
        key = (r.get("dataset_id"), r.get("benchmark_id"), r.get("metric_id"), r.get("split"))
        groups.setdefault(key, []).append(r)
    out = {}
    for key, rows in groups.items():
        m = rv.tables["metrics"].get(key[2]) or {}
        hib = m.get("higher_is_better")
        rows.sort(key=lambda r: (-_num(r["value"]) if hib in (True, None) else _num(r["value"])))
        for i, r in enumerate(rows):
            out[r["id"]] = (i + 1, len(rows))
    return out


# --------------------------------------------------------------------------
# step 6: KPIs and paper ranking (derived, never stored)
# --------------------------------------------------------------------------

def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
        return float(v)
    return None


def _in_percent(units) -> bool:
    # "%", "AUC (%)", "percent"; also 0-100 score scales ("points (0-100)", "ROUGE x100"), which llm-to-domain-slm's
    # ROUGE-2 board mixed with '%' rows
    return bool(re.search(r"%|\bpercent|\b0\s*[-–]\s*100\b|[x×]\s?100\b", str(units or ""), re.I))


def _kpi_value(r: dict) -> float | None:
    """A result value on a common scale: rows stored in percent (units '%') are divided by 100, so a
    paper that prints AbsRel 4.5 (%) ranks against one that prints 0.045."""
    v = _num(r.get("value"))
    if v is not None and _in_percent(r.get("units")):
        return v / 100.0
    return v


def _board_in_percent(board: list[dict]) -> bool:
    """A KPI board is drawn and spoken in percent when any of its rows is stored in percent: one board, one scale
    (MDE showed "0.044" beside "3.42", and its writers said "5.78%" and "0.0421" for rows of one board)."""
    return any(_in_percent(e.get("units")) for e in board)


def _shown_value(e: dict, in_pct: bool) -> float | None:
    """A board entry's value on its board's scale (`value` is the common scale `_kpi_value` ranks on)."""
    v = e.get("value")
    return float(f"{v * 100 if in_pct else v:.6g}") if isinstance(v, (int, float)) else None


def _shown_text(e: dict, in_pct: bool) -> str | None:
    v = _shown_value(e, in_pct)
    return None if v is None else f"{v:g}{'%' if in_pct else ''}"


def _rankable_paper(p: dict) -> bool:
    return (p.get("status") == "included" and not p.get("taxonomy_source") and not p.get("is_survey")
            and (p.get("paper_role") or "method") == "method")


def _role_exempt(p: dict) -> bool:
    """Included non-survey paper whose paper_role says it is not a method (evaluation/benchmark/survey study)."""
    return (p.get("status") == "included" and not p.get("taxonomy_source") and not p.get("is_survey")
            and (p.get("paper_role") or "method") != "method")


_BASELINE_ROW = re.compile(r"\bbaseline (re-)?reported\b", re.I)
ROW_KINDS = ("own", "ablation", "baseline", "oracle")
# rows step-5 writers stored before `row_kind` existed: an oracle / upper bound / human reference and an ablation
# ("w/o X") never rank their paper (table-qa 2026-10-29: TabAF's "Upper Bound" row ranked it first on WTQ and TabFact,
# TableMaster's "w/o Column Lookup" ablation scored 90.51 on TabFact against its full model's 90.12)
_ORACLE_ROW = re.compile(r"\boracle\b|\bupper[- ]?bound\b|\bhuman (?:performance|expert|baseline)\b", re.I)
_ABLATION_ROW = re.compile(r"\bablation\b|\bw/o\b", re.I)


def _row_kind(r: dict) -> str:
    """own | ablation | baseline | oracle. The step-5 `row_kind` field when set, else read from caveats/method_variant."""
    k = r.get("row_kind")
    if k in ROW_KINDS:
        return k
    cav, mv = str(r.get("caveats") or ""), str(r.get("method_variant") or "")
    if _BASELINE_ROW.search(cav):
        return "baseline"
    if _ORACLE_ROW.search(mv) or _ORACLE_ROW.search(cav):
        return "oracle"
    if _ABLATION_ROW.search(mv) or re.search(r"\bablation\b", cav, re.I):
        return "ablation"
    return "own"


def _is_baseline_row(r: dict) -> bool:
    return _row_kind(r) == "baseline"


def _usable_result(rv: Review, r: dict) -> bool:
    """A row that can rank its paper: the paper's own method (not a baseline it re-reports, an ablation or an
    oracle / upper-bound reference), positive status, numeric."""
    p = rv.tables["papers"].get(r.get("paper_id") or "")
    if _row_kind(r) != "own":
        return False
    return bool(p) and _rankable_paper(p) and r.get("status") in POSITIVE and _num(r.get("value")) is not None


_SETTING_MODEL = re.compile(r"\b(?:llama|qwen|mistral|mixtral|gemma|phi|gpt|opt|bloom|deepseek|codellama|falcon|pythia|t5|bert)"
                            r"[\w.\-]*?(?:\s+[\d.]+)?[\s\-]?\d+(?:\.\d+)?b\b", re.I)      # "LLaMA2-7B", "Llama 3.1 8B"


def _setting_tokens(mv) -> set:
    """The base model and the sparsity or bit-width a method variant names ('LLaMA-2-7B', '50%', 'W4A16'), normalised:
    two rows of one paper share a benchmark's setting when the board row's tokens are all in the other's."""
    mv = str(mv or "")
    toks = {norm_name(m) for m in _SETTING_MODEL.findall(mv)} | set(re.findall(r"\b\d{1,3}(?:\.\d+)?%", mv))
    toks |= {m.upper() for m in re.findall(r"\bW\d+A\d+\b", mv, re.I)}
    toks |= {f"{round(float(m) * 100):d}%" for m in re.findall(r"\b(?:p|sparsity)\s*=\s*(0?\.\d+)\b", mv, re.I)}   # ATP: p=0.5
    return toks


def _kpi_rows(rv: Review, k: dict) -> list[dict]:
    splits = k.get("splits") or []
    out = []
    for r in rv.tables["results"].values():
        if k.get("benchmark_id"):
            if r.get("benchmark_id") != k["benchmark_id"]:
                continue
        elif r.get("dataset_id") != k.get("dataset_id"):
            continue
        if r.get("metric_id") != k.get("metric_id") or r.get("split") not in splits:
            continue
        if k.get("metric_variant") is not None and r.get("metric_variant") != k["metric_variant"]:
            continue
        if _usable_result(rv, r):
            out.append(r)
    return out


def kpi_coverage(rv: Review, k: dict) -> int:
    return len({r["paper_id"] for r in _kpi_rows(rv, k)})


def kpi_suggest(rv: Review) -> list[dict]:
    """Every (benchmark|dataset, metric) board with distinct included papers per split label, most covered first."""
    groups: dict[tuple, dict] = {}
    variants: dict[tuple, set] = {}
    for r in rv.tables["results"].values():
        tgt = r.get("benchmark_id") or r.get("dataset_id")
        if not tgt or not r.get("metric_id") or not _usable_result(rv, r):
            continue
        groups.setdefault((tgt, r["metric_id"]), {}).setdefault(r.get("split"), set()).add(r["paper_id"])
        variants.setdefault((tgt, r["metric_id"]), set()).add(r.get("metric_variant"))
    out = []
    for (tgt, mid), sp in groups.items():
        e = rv.tables["benchmarks" if tgt.startswith("B") else "datasets"].get(tgt) or {}
        m = rv.tables["metrics"].get(mid) or {}
        out.append({"target": tgt, "target_name": e.get("name"), "metric_id": mid, "metric_name": m.get("name"),
                    "higher_is_better": m.get("higher_is_better"), "papers": len(set().union(*sp.values())),
                    "metric_variants": sorted(str(v) for v in variants[(tgt, mid)] if v is not None),
                    "splits": sorted(({"split": s, "papers": len(v)} for s, v in sp.items()),
                                     key=lambda x: (-x["papers"], str(x["split"])))})
    out.sort(key=lambda g: (-g["papers"], g["target"], g["metric_id"]))
    return out


def kpi_add(rv: Review, benchmark_id: str | None, dataset_id: str | None, metric_id: str, splits: list, rationale: str,
            metric_variant: str | None = None) -> dict:
    K = rv.meta.setdefault("kpis", [])
    if len(K) >= KPI_MAX:
        raise SystemExit(f"max {KPI_MAX} KPIs; remove one first (kpi --remove)")
    if bool(benchmark_id) == bool(dataset_id):
        raise SystemExit("give exactly one of --benchmark / --dataset")
    if benchmark_id and benchmark_id not in rv.tables["benchmarks"]:
        raise SystemExit(f"{benchmark_id} not in benchmarks")
    if dataset_id and dataset_id not in rv.tables["datasets"]:
        raise SystemExit(f"{dataset_id} not in datasets")
    m = rv.tables["metrics"].get(metric_id or "")
    if m is None:
        raise SystemExit(f"{metric_id} not in metrics")
    if m.get("higher_is_better") not in (True, False):
        raise SystemExit(f"metric {metric_id} has no direction (higher_is_better is null); set it before using it as a KPI")
    if not rationale or not rationale.strip():
        raise SystemExit("a rationale is required: why this board, and why these split labels are one split")
    if not splits:
        raise SystemExit("give at least one --split (use 'none' for rows without a split label)")
    k = {"benchmark_id": benchmark_id, "dataset_id": dataset_id, "metric_id": metric_id, "metric_variant": metric_variant,
         "splits": list(splits), "rationale": rationale.strip()}
    for old in K:
        if all(old.get(f) == k[f] for f in ("benchmark_id", "dataset_id", "metric_id", "metric_variant")) and sorted(map(str, old["splits"])) == sorted(map(str, k["splits"])):
            raise SystemExit(f"this KPI already exists as {old['id']}")
    n = kpi_coverage(rv, k)
    if n < KPI_MIN_PAPERS:
        raise SystemExit(f"this KPI covers {n} included papers; it needs at least {KPI_MIN_PAPERS} (a board with fewer papers makes rank meaningless)")
    rec = {"id": rv.new_id("K"), **k}
    K.append(rec)
    return rec


def kpi_remove(rv: Review, kid: str) -> None:
    K = rv.meta.get("kpis") or []
    if not any(k["id"] == kid for k in K):
        raise SystemExit(f"{kid} is not a KPI")
    rv.meta["kpis"] = [k for k in K if k["id"] != kid]


def rank_papers(rv: Review) -> dict:
    """Rank included non-survey papers by their best rank across the KPIs (credited to the best variant).
    Sort: best rank -> more KPIs covered -> published venue before preprint -> rank on K1, K2, … -> verified citations
    -> id. Pure; never stored."""
    K = rv.meta.get("kpis") or []
    top_n = int(rv.meta.get("top_n") or 10)
    res = {"kpis": [k["id"] for k in K], "top_n": top_n, "ranked": [], "below_cut": [], "unranked": [], "warnings": [], "notes": []}
    if not K:
        return res
    papers, metrics = rv.tables["papers"], rv.tables["metrics"]
    per: dict[str, dict] = {}
    for k in K:
        hib = (metrics.get(k["metric_id"]) or {}).get("higher_is_better")
        if hib not in (True, False):
            res["warnings"].append(f"{k['id']} skipped: metric {k['metric_id']} has no direction (higher_is_better is null)")
            continue
        best: dict[str, tuple] = {}
        rows_k = _kpi_rows(rv, k)
        n_k = len({r["paper_id"] for r in rows_k})
        if n_k < KPI_FEW_PAPERS:
            res["notes"].append(f"{k['id']}: only {n_k} paper(s) on this board - a rank on it rests on few papers; say so wherever it is used")
        variants = sorted({str(r.get("metric_variant")) for r in rows_k if r.get("metric_variant") is not None})
        if k.get("metric_variant") is None and len(variants) > 1:
            res["warnings"].append(f"{k['id']}: rows carry {len(variants)} metric_variant values ({', '.join(variants[:5])}); each paper is "
                                   f"credited its best one - re-add the KPI with --metric-variant to rank one threshold")
        vals = [_kpi_value(r) for r in rows_k]
        big, small = [v for v in vals if v > 1.5], [v for v in vals if 0 < v <= 1]
        if big and small and max(big) >= 20 * max(small):   # 60.4 vs 0.626 = percent vs fraction; 2.3 m vs 0.8 m is not
            res["warnings"].append(f"{k['id']}: mixed scales on one board (values above 1 and in [0, 1] after % normalisation) - "
                                   f"check units on its rows (a percent row without units '%' ranks as 100x)")
        for r in rows_k:
            v, cur = _kpi_value(r), best.get(r["paper_id"])
            if cur is None or (v > cur[0] if hib else v < cur[0]) or (v == cur[0] and r["id"] < cur[1]["id"]):
                best[r["paper_id"]] = (v, r)
        if k.get("benchmark_id"):
            # a hand-pointed benchmark board can miss a paper's better own row of the same test set, metric and split
            # (EfficientXpert's ForeSight row, 72.86% on MedNLI, sat beside the board while its 59.7% PBS row ranked it
            # third; an outside reader found it through a script writer, llm-to-domain-slm 2026-10-26)
            dsets = {r.get("dataset_id") for r in rows_k if r.get("dataset_id")}
            for pid, (v, br) in best.items():
                want = _setting_tokens(br.get("method_variant"))
                for r in papers.get(pid, {}).get("result_ids") or []:
                    x = rv.tables["results"].get(r) or {}
                    if (x.get("benchmark_id") or x.get("dataset_id") not in dsets or x.get("metric_id") != k["metric_id"]
                            or x.get("split") != br.get("split") or not _usable_result(rv, x)
                            or not want or not want <= _setting_tokens(x.get("method_variant"))
                            or re.search(r"\bablation\b|\boracle\b", f"{x.get('method_variant')} {x.get('caveats')}", re.I)
                            or x.get("metric_variant") != (k.get("metric_variant") or br.get("metric_variant"))):
                        continue
                    xv = _kpi_value(x)
                    if (xv > v) if hib else (xv < v):
                        res["warnings"].append(f"{k['id']}: {pid}'s own row {x['id']} ({x.get('value')}) has the board's test set, "
                                               f"metric and split and beats its board row {br['id']} ({br.get('value')}) but is not on "
                                               f"{k['benchmark_id']}: point it there if its setting matches the benchmark")
        order = sorted(best.items(), key=lambda kv: -kv[1][0] if hib else kv[1][0])
        rank, prev = 0, None
        for i, (pid, (v, r)) in enumerate(order):
            if v != prev:
                rank, prev = i + 1, v
            per.setdefault(pid, {})[k["id"]] = {"rank": rank, "denominator": len(order), "result_id": r["id"],
                                                 "method_variant": r.get("method_variant"), "value": v,
                                                 "raw_value": r.get("value"), "units": r.get("units")}
    kids = [k["id"] for k in K]

    def cites(pid: str) -> float:
        c = papers[pid].get("citations")
        v = c.get("value") if is_evidence(c) and c.get("status") == "verified" else None
        return v if isinstance(v, (int, float)) else 0

    rows = []
    for pid, d in per.items():
        bk = min(kids, key=lambda kid: (d[kid]["rank"] if kid in d else 10 ** 9, kids.index(kid)))
        rows.append({"paper_id": pid, "best_kpi": bk, "best_rank": d[bk]["rank"], "denominator": d[bk]["denominator"],
                     "result_id": d[bk]["result_id"], "method_variant": d[bk]["method_variant"], "per_kpi": d,
                     "venue": published_venue_label(papers[pid])})
    # a published venue breaks ties right after KPI rank and coverage: it never lifts a paper over a better KPI rank
    rows.sort(key=lambda r: (r["best_rank"], -len(r["per_kpi"]), r["venue"] is None,
                             *[r["per_kpi"].get(k, {}).get("rank", 10 ** 9) for k in kids], -cites(r["paper_id"]), r["paper_id"]))
    for i, r in enumerate(rows):
        r["position"] = i + 1
    res["ranked"], res["below_cut"] = rows[:top_n], rows[top_n:]
    # a ranked paper whose best row names another method is usually a comparison method the authors ran themselves,
    # stored without `row_kind: baseline` (table-qa 2026-10-29: TableMaster ranked on its own "Direct (o1-preview)" row)
    for r in res["ranked"]:
        short = _own_method_name(papers[r["paper_id"]])
        for kid, e in sorted(r["per_kpi"].items()):
            row = rv.tables["results"].get(e["result_id"]) or {}
            if short and row.get("row_kind") != "own" and short not in re.sub(r"[^a-z0-9]", "", str(e["method_variant"] or "").lower()):
                res["notes"].append(f"{r['paper_id']}: its {kid} row {e['result_id']} ({e['method_variant']!r}) does not name the paper's "
                                    f"own method ({papers[r['paper_id']]['title'].split(':')[0].strip()!r}) - if it is a comparison "
                                    f"method the authors ran, set its row_kind to baseline")
    res["unranked"] = [{"paper_id": p["id"], "reason": "no result on " + "/".join(kids) + _boards_reported(rv, p["id"])}
                       for p in sorted(papers.values(), key=lambda p: p["id"]) if _rankable_paper(p) and p["id"] not in per]
    # evaluation / benchmark studies: their best row belongs to someone else's method - listed, never ranked
    res["unranked"] += [{"paper_id": p["id"], "role": p["paper_role"],
                         "reason": f"{p['paper_role']} study (not ranked): {p.get('paper_role_reason') or 'no reason given'}"}
                        for p in sorted(papers.values(), key=lambda p: p["id"]) if _role_exempt(p)]
    return res


def _own_method_name(p: dict) -> str | None:
    """The paper's method name from a 'Name: subtitle' title when the prefix is one word ("TableMaster", "Table-R1",
    "Chain-of-Table"), normalised to lower-case letters and digits; None otherwise ("Beyond Linearization: ..." names no
    method - TabGR - and gave a false note on the table-qa replay)."""
    head = str(p.get("title") or "").split(":")[0].strip()
    if ":" not in str(p.get("title") or "") or not head or len(head.split()) != 1:
        return None
    n = re.sub(r"[^a-z0-9]", "", head.lower())
    return n if len(n) >= 3 else None


def _boards_reported(rv: Review, pid: str, n: int = 3) -> str:
    """'; reports on <target> <metric> (k rows), ...' for the boards an unranked paper does have rows on."""
    cnt: dict[tuple, int] = {}
    for r in rv.tables["results"].values():
        tgt = r.get("benchmark_id") or r.get("dataset_id")
        if r.get("paper_id") == pid and tgt and r.get("metric_id") and _usable_result(rv, r):
            cnt[(tgt, r["metric_id"])] = cnt.get((tgt, r["metric_id"]), 0) + 1
    if not cnt:
        return "; no usable own result rows"
    top = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    name = lambda t: (rv.tables["benchmarks" if t.startswith("B") else "datasets"].get(t) or {}).get("name") or t
    return "; reports on " + ", ".join(f"{name(t)} {(rv.tables['metrics'].get(m) or {}).get('name') or m} ({c} rows)" for (t, m), c in top)


def _flat(v) -> str:
    if v is None:
        return ""
    if is_evidence(v):
        val = v.get("value")
        if isinstance(val, dict):
            val = "; ".join(f"{k}={x}" for k, x in val.items() if x not in (None, "", []))
        elif isinstance(val, list):
            val = "; ".join(str(x) for x in val)
        tail = v.get("status", "")
        if v.get("source"):
            tail += f" | {v['source']}"
        if v.get("location"):
            tail += f" @ {v['location']}"
        if v.get("rationale"):
            tail += f" | {v['rationale']}"
        return f"{val if val is not None else ''} [{tail}]".strip()
    if isinstance(v, dict):
        return "; ".join(f"{k}={_flat(x)}" for k, x in v.items() if x not in (None, "", []))
    if isinstance(v, list):
        return "; ".join(_flat(x) for x in v)
    return str(v)


def _rows(rv: Review, table: str) -> tuple[list[str], list[list[str]]]:
    data = rv.tables[table]
    recs = data if isinstance(data, list) else list(data.values())
    cols: list[str] = []
    for r in recs:
        for k in r:
            if k not in cols:
                cols.append(k)
    if table == "results":
        ranks = compute_set_ranks(rv)
        for r in recs:
            rk = ranks.get(r["id"])
            r["_rank_in_reviewed_set"] = f"{rk[0]}/{rk[1]}" if rk else ""
        if "_rank_in_reviewed_set" not in cols:
            cols.append("_rank_in_reviewed_set")
    def cell(r, c):
        v = r.get(c)
        if table == "papers" and c == "figures" and is_evidence(v) and isinstance(v.get("value"), list):
            # full figure records (URLs, 600-char captions) belong in the hub, not in a table cell
            return _flat({**v, "value": f"{len(v['value'])} figures: " + ", ".join(f.get("label", "") for f in v["value"])})
        return _flat(v)
    rows = [[tex_to_text(cell(r, c)) for c in cols] for r in recs]
    if table == "results":
        for r in recs:
            r.pop("_rank_in_reviewed_set", None)
    return cols, rows


def _md_table(cols, rows) -> str:
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def embed_explainer_figures(rv: Review) -> dict:
    """Inline each explainer's chosen figure as a data: URI within the size budget; otherwise keep a link and say why."""
    out, total = {}, 0
    for x in sorted(rv.tables["explainers"].values(), key=lambda x: x["id"]):
        p = rv.tables["papers"].get(x.get("paper_id") or "") or {}
        pf = p.get("figures")
        figs = pf["value"] if is_evidence(pf) and pf.get("status") in POSITIVE else []
        fig = x.get("figure") or {}
        f = next((g for g in figs if g["label"] == fig.get("label")), None)
        if f is None:
            note = "no figure chosen" if figs else f"paper figures not available ({pf.get('status') if is_evidence(pf) else 'not extracted'})"
            out[x["id"]] = {"src": None, "link": None, "caption": None, "note": note}
            continue
        st, data, ctype = fetch_bytes(f["url"], rv.cache_dir)
        src, note = None, None
        if st != 200 or not data:
            note = f"fetch failed (HTTP {st})"
        elif not str(ctype or "").startswith("image/"):
            note = f"not an image ({ctype})"
        elif len(data) > FIG_MAX_BYTES:
            note = f"{len(data) // 1024} KB is over the {FIG_MAX_BYTES // 1000} KB per-figure budget"
        elif total + len(data) > FIG_TOTAL_BYTES:
            note = f"over the {FIG_TOTAL_BYTES // 1_000_000} MB total figure budget"
        else:
            src = f"data:{ctype or 'image/png'};base64," + base64.b64encode(data).decode()
            total += len(data)
        out[x["id"]] = {"src": src, "link": f["url"], "caption": f["caption"], "note": note}
    return out


RT_MAX_COLS, RT_MAX_ROWS = 10, 20


def _paper_table(loc) -> str | None:
    m = re.match(r"\s*(Table\s*[A-Za-z]?\d+[a-z]?)\b", loc or "", re.I)
    return re.sub(r"\s+", " ", m.group(1)).title() if m else None


def _segments(label: str) -> list[str]:
    """'A (retriever BM25), LLM=X (11B)' -> ['A', 'retriever BM25', 'LLM=X (11B)']: top-level comma/semicolon parts, with the
    first part's trailing parenthetical split off as a qualifier."""
    parts, depth, cur = [], 0, ""
    for ch in label:
        depth += (ch == "(") - (ch == ")")
        if ch in ",;" and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    out: list[str] = []
    for p in parts:
        m = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", p)
        out += [m.group(1), m.group(2)] if m and not out else [p]
    return [s for s in out if s]


def _join_segments(segs: list[str]) -> str:
    return segs[0] + (f" ({', '.join(segs[1:])})" if len(segs) > 1 else "") if segs else ""


def _method_key(name) -> str:
    """'HC-Net [44]' / 'FG$^2$*' / 'VIRD (Ours)' -> 'hcnet' / 'fg2' / 'vird': a method name compared across papers."""
    s = _segments(str(name or ""))[0] if name else ""
    s = re.sub(r"\[[\d,\s–-]*\]|[*†‡]", "", s)
    s = re.sub(r"\bours\b", "", s, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _method_papers(rv: Review) -> dict:
    """Method name key -> paper ids that introduce it: the title prefix before ':' and, for included papers, rows the
    paper labels as its own ('HC-Net (Ours)'). Unmarked rows are not used: re-run baselines often lack the baseline
    caveat. Included papers are tried first, then every other paper by its title prefix."""
    T, inc, rest = rv.tables, {}, {}
    own: dict = {}
    for r in T["results"].values():
        if re.search(r"\bours\b", str(r.get("method_variant") or ""), re.I) and not _is_baseline_row(r):
            own.setdefault(r.get("paper_id"), set()).add(_method_key(r.get("method_variant")))
    words: dict = {}
    for p in T["papers"].values():
        t = str(p.get("title") or "")
        keys = {_method_key(t[:t.index(":")])} if 1 < t.find(":") < 42 else set()
        if p.get("status") == "included":
            keys |= own.get(p["id"], set())
            ws = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in re.split(r"[\s:/]+", t)]
            for i in range(len(ws)):
                for j in range(i + 1, min(i + 3, len(ws)) + 1):
                    if len(k := "".join(ws[i:j])) >= 4:
                        words.setdefault(k, set()).add(p["id"])
        for k in keys - {""}:
            (inc if p.get("status") == "included" else rest).setdefault(k, set()).add(p["id"])
    return {"included": inc, "other": rest, "words": words}


def _method_paper(mp: dict, label, not_pid: str | None = None) -> str | None:
    """The one paper a baseline row's method names, or None when no paper (or more than one) matches. Last tier: the
    name as one to three whole words of exactly one included paper's title ('DenseFlow' -> 'Learning Dense Flow Field
    ...'); 'SelfC' does not match 'Self-Conditioned', and keys under 4 characters never match title words."""
    k = _method_key(label)
    for tier in ("included", "other", "words"):
        hit = mp.get(tier, {}).get(k, set()) - {not_pid}
        if hit:
            return next(iter(hit)) if len(hit) == 1 else None
    return None


def _method_name(label) -> str:
    """'DenseFlow [37]' / 'G2SWeakly (lambda1=0)' / 'DenseFlow*' -> 'DenseFlow': the bare name a Results-tab filter uses."""
    s = _segments(str(label or ""))[0] if label else ""
    s = re.sub(r"\s+", " ", re.sub(r"\[[\d,\s–-]*\]|[*†‡]", "", s)).strip()
    return s if len(s) >= 3 else str(label or "").strip()           # 'P (true)': 'P' would match every row


def _is_later(p: dict, w: dict) -> bool:
    """p appeared after w: by arXiv id (YYMM.NNNNN orders by submission) when both have one, else by year."""
    a, b = (p.get("ids") or {}).get("arxiv"), (w.get("ids") or {}).get("arxiv")
    if a and b and re.fullmatch(r"\d{4}\.\d{4,5}", a) and re.fullmatch(r"\d{4}\.\d{4,5}", b):
        return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))
    return (p.get("year") or 0) > (w.get("year") or 0)


def _later_on_kpis(rv: Review, rp: dict, pid: str) -> list:
    """For each KPI board the paper is on: its own best row, then every later paper's best row on that board."""
    T, out = rv.tables, []
    rows = rp["ranked"] + rp["below_cut"]
    me = next((r for r in rows if r["paper_id"] == pid), None)
    w = T["papers"].get(pid) or {}
    for k in rv.meta.get("kpis") or []:
        if not me or k["id"] not in me["per_kpi"]:
            continue
        ents = []
        for r in rows:
            q = r["per_kpi"].get(k["id"])
            p = T["papers"].get(r["paper_id"]) or {}
            if q is None or not (r["paper_id"] == pid or _is_later(p, w)):
                continue
            res = T["results"].get(q["result_id"]) or {}
            ents.append({"paper_id": r["paper_id"], "title": p.get("title"), "year": p.get("year"),
                         "arxiv": (p.get("ids") or {}).get("arxiv"), "ref": r["paper_id"] == pid,
                         "method_variant": q.get("method_variant"), "split": res.get("split"),
                         "metric_variant": res.get("metric_variant"), "value": q.get("raw_value"), "units": q.get("units"),
                         "rank": q["rank"], "denominator": q["denominator"], "result_id": q["result_id"]})
        ents.sort(key=lambda e: e["rank"])
        out.append({"kpi_id": k["id"], "label": _kpi_label(rv, k), "rows": ents,
                    "n_later": sum(1 for e in ents if not e["ref"])})
    return out


def explainer_result_tables(rv: Review, rp: dict | None = None) -> dict:
    """Per explainer: the winner's own comparison table (its rows next to the baselines it re-reports), pivoted to
    methods x settings. Built only from `results`; a winner whose extracted rows hold no comparison gets a note."""
    rp = rp or rank_papers(rv)
    T, out = rv.tables, {}
    rk = {r["paper_id"]: r for r in rp["ranked"] + rp["below_cut"]}
    mp = _method_papers(rv)
    for x in sorted(T["explainers"].values(), key=lambda x: x["id"]):
        pid = x.get("paper_id")
        rows = sorted((r for r in T["results"].values() if r.get("paper_id") == pid and r.get("provenance") != "leaderboard-verified"
                       and r.get("value") is not None), key=lambda r: r["id"])
        anchor = T["results"].get((rk.get(pid) or {}).get("result_id") or "")
        by_tab: dict = {}
        for r in rows:
            t = _paper_table(r.get("location"))
            if t:
                by_tab.setdefault(t, []).append(r)

        def n_base(t):
            return len({r.get("method_variant") for r in by_tab[t] if _is_baseline_row(r)})

        def has(t, f):
            return anchor is not None and any(r.get(f) == anchor.get(f) for r in by_tab[t])
        cands = [t for t in by_tab if n_base(t)]
        a_tab = _paper_table((anchor or {}).get("location"))
        if a_tab in cands:
            tab, why = a_tab, "the paper's table that holds its best KPI result"
        elif cands:
            tab = max(cands, key=lambda t: (has(t, "metric_id") and has(t, "dataset_id"), has(t, "metric_id"), n_base(t), -list(by_tab).index(t)))
            why = (f"its best KPI result is in {a_tab}, which compares no other methods" if a_tab else "its best KPI result names no table") \
                + f"; {tab} is its comparison table closest to that KPI"
        else:
            out[x["id"]] = {"paper_id": pid, "table": None, "later": _later_on_kpis(rv, rp, pid),
                            "note": "no comparison table in the extracted results: none of this paper's result rows is a baseline "
                                    "it re-reports (step 5 marks those with the caveat 'baseline re-reported by …')"}
            continue
        trs = by_tab[tab]

        def dsid(r):
            if r.get("dataset_id"):
                return r["dataset_id"]
            b = T["benchmarks"].get(r.get("benchmark_id") or "") or {}
            return (b.get("dataset_ids") or [None])[0] or r.get("benchmark_id")

        def ckey(r):
            return (dsid(r), r.get("split"), r.get("metric_id"), r.get("metric_variant"))
        cols = list(dict.fromkeys(ckey(r) for r in trs))
        ak = ckey(anchor) if anchor is not None and anchor in trs else None
        ref = ak or cols[0]
        order = {c: i for i, c in enumerate(cols)}
        cols.sort(key=lambda c: (c != ak, c[2] != ref[2], c[0] != ref[0], c[1] != ref[1], order[c]))

        # drop qualifiers a row's metric_variant already states ("NQC (retriever BM25)" under "…, retriever BM25" columns)
        def strip(r):
            segs = _segments(str(r.get("method_variant") or "variant not stated"))
            mv = [str(q.get("metric_variant") or "").lower() for q in trs if q.get("method_variant") == r.get("method_variant")]
            return [s for i, s in enumerate(segs) if i == 0 or not all(s.lower() in v for v in mv)]
        labels = {r["id"]: strip(r) for r in trs}
        if len({(_join_segments(labels[r["id"]]), ckey(r)) for r in trs}) < len({(r.get("method_variant"), ckey(r)) for r in trs}):
            labels = {r["id"]: _segments(str(r.get("method_variant") or "variant not stated")) for r in trs}   # would merge cells
        common_r = [s for s in labels[trs[0]["id"]][1:] if all(s in labels[r["id"]][1:] for r in trs)]
        mvs = [_segments(str(c[3])) if c[3] else [] for c in cols]
        common_c = [s for s in mvs[0] if all(s in m for m in mvs)] if len(cols) > 1 else []
        split_n = {}
        for c in cols:
            split_n[c[1]] = split_n.get(c[1], 0) + 1
        main_split = max(split_n, key=lambda k: split_n[k])
        if split_n[main_split] * 2 <= len(cols):   # no split covers most columns: label every column with its own
            main_split = object()
        M, D, B = T["metrics"], T["datasets"], T["benchmarks"]
        shown = cols[:RT_MAX_COLS]
        col_out = []
        for c in shown:
            m = M.get(c[2] or "") or {}
            mname = str(m.get("name") or c[2])
            mv = ", ".join(s for s in (_segments(str(c[3])) if c[3] else []) if s not in common_c and s.lower() not in mname.lower())
            col_out.append({"dataset": (D.get(c[0] or "") or B.get(c[0] or "") or {}).get("name") or c[0],
                            "metric": mname, "variant": mv or None,
                            "split": c[1] if c[1] != main_split else None, "higher_is_better": m.get("higher_is_better"),
                            "anchor": c == ak})
        meth: dict = {}
        for r in trs:
            lab = _join_segments([s for s in labels[r["id"]] if s not in common_r]) or labels[r["id"]][0]
            e = meth.setdefault(lab, {"label": lab, "own": True, "oracle": bool(re.search(r"\boracle\b", lab, re.I)),
                                      "result_ids": [], "cells": {}})
            e["own"] = e["own"] and not _is_baseline_row(r)
            e["result_ids"].append(r["id"])
            e["cells"].setdefault(ckey(r), r)
        mrows = list(meth.values())
        if len(mrows) > RT_MAX_ROWS:   # keep every own row, then the baselines that report the lead column
            keep = ([m for m in mrows if m["own"]] + [m for m in mrows if not m["own"] and shown[0] in m["cells"]])[:RT_MAX_ROWS]
            mrows = [m for m in mrows if m in keep]
        best = {}
        for c, co in zip(shown, col_out):
            vals = [m["cells"][c]["value"] for m in mrows if c in m["cells"] and isinstance(m["cells"][c]["value"], (int, float))
                    and not m["oracle"]]
            if len(vals) > 1 and co["higher_is_better"] is not None:
                best[c] = max(vals) if co["higher_is_better"] else min(vals)
        row_out = [{"label": m["label"], "own": m["own"], "paper_id": pid if m["own"] else _method_paper(mp, m["label"], pid), "name": _method_name(m["label"]), "oracle": m["oracle"], "anchor": anchor is not None and anchor["id"] in m["result_ids"],
                    "cells": [None if c not in m["cells"] else
                              {"value": m["cells"][c]["value"], "result_id": m["cells"][c]["id"],
                               "best": not m["oracle"] and c in best and best[c] == m["cells"][c]["value"]} for c in shown]} for m in mrows]
        units = {r.get("units") for r in trs}
        out[x["id"]] = {"paper_id": pid, "table": tab, "why": why, "later": _later_on_kpis(rv, rp, pid),
                        "common": ", ".join(common_r + common_c + ([f"split {main_split}" + (" unless the column says otherwise" if len(split_n) > 1 else "")]
                                                                   if isinstance(main_split, str) and main_split else [])) or None,
                        "units": next(iter(units)) if len(units) == 1 else None,
                        "columns": col_out, "rows": row_out,
                        "more_columns": len(cols) - len(shown), "more_rows": len(meth) - len(mrows),
                        "n_baselines": sum(1 for m in mrows if not m["own"])}
    return out


def _step6_md(rv: Review) -> list[str]:
    K = rv.meta.get("kpis") or []
    if not K:
        return []
    T, rp = rv.tables, rank_papers(rv)
    lines = ["## Ranking & clusters", ""]
    for k in K:
        tgt = T["benchmarks" if k.get("benchmark_id") else "datasets"].get(k.get("benchmark_id") or k.get("dataset_id") or "") or {}
        m = T["metrics"].get(k["metric_id"]) or {}
        v = f" [{k['metric_variant']}]" if k.get("metric_variant") else ""
        lines.append(f"- {k['id']}: {tgt.get('name')} · {m.get('name')}{v} · splits {k['splits']} — {k['rationale']}")
    lines += ["", f"Top {rp['top_n']}:", ""]
    for r in rp["ranked"]:
        lines.append(f"{r['position']}. {r['paper_id']} {T['papers'][r['paper_id']].get('title')} ({venue_text(T['papers'][r['paper_id']])}) — rank {r['best_rank']}/{r['denominator']} on {r['best_kpi']} ({r.get('method_variant') or 'variant not stated'})")
    for u in rp["unranked"]:
        lines.append(f"- unranked: {u['paper_id']} — {u['reason']}")
    lines.append("")
    for c in sorted(T["clusters"].values(), key=lambda c: c["id"]):
        x = next((e for e in T["explainers"].values() if e.get("cluster_id") == c["id"]), None)
        wp = T["papers"].get(c.get("winner_paper_id") or "") or {}
        lines.append(f"- {c['id']} {c.get('name')}: {', '.join(c.get('paper_ids') or [])} — winner {c.get('winner_paper_id')}"
                     + (f" ({venue_text(wp)})" if wp else "")
                     + (f" — explainer {x['id']}: {x.get('headline')}" if x else " — no explainer yet"))
        if x:
            dl = [e for e in x.get("declared_limitations") or [] if e.get("value")]
            if dl or x.get("weak_points"):
                lines.append("  - Limitations the authors state: " + ("; ".join(_md_emph(e["value"], e.get("emphasis")) for e in dl) if dl else "none stated"))
            lines += [f"  - Weak point ({e.get('kind') or 'other'}, reviewer's reading): {_md_emph(e['value'], e.get('emphasis'))}"
                      for e in x.get("weak_points") or []]
    syn = (rv.meta.get("synthesis") or {}).get("takeaways") or []
    if syn:
        lines += ["", "What the families add up to:", ""]
        lines += [f"- {_md_emph(t['text'], t.get('emphasis'))} ({', '.join(e['paper_id'] for e in t['evidence'])})" for t in syn]
    return lines + _limitations_md(rv) + [""]


LIMIT_MARKS = {("affected", "author-reported"): "● authors", ("affected", "inferred"): "◐ reviewer",
               ("addresses", "author-reported"): "✓ addresses"}


def _limitations_md(rv: Review) -> list[str]:
    lim = rv.meta.get("limitations") or {}
    P, C = lim.get("problems") or [], sorted(rv.tables["clusters"])
    if not P:
        return []
    b = lim.get("basis") or {}
    lines = ["", "Limitations and problems of the current approaches:", "",
             f"Based on the critiques of the {b.get('winners', 0)} family winners and the limitations that "
             f"{b.get('members', 0)} other family members state. ● the authors say so, ◐ the reviewer's reading, "
             "✓ the family addresses it, — not shown.", "",
             "| Problem | Kind | " + " | ".join(C) + " |", "|---|---|" + "---|" * len(C)]
    for t in P:
        cells = [LIMIT_MARKS.get(((t["cells"].get(c) or {}).get("state"), (t["cells"].get(c) or {}).get("basis")), "—") for c in C]
        lines.append(f"| {t['id']} {t['title'].replace('|', '/')} | {t['kind']} | " + " | ".join(cells) + " |")
    lines.append("")
    for t in P:
        ev = [f"{c} {x['paper_id']}" for c, x in t["cells"].items() if x.get("state") != "not-shown"]
        lines.append(f"- {t['id']} {t['title']}: {_md_emph(t['text'], t.get('emphasis'))} ({'; '.join(ev)})")
    if lim.get("open_problems"):
        lines += ["", "Open problems no reviewed approach solves (from general knowledge of the field, not from the "
                      "reviewed papers; not checked against any source):", ""]
        lines += [f"- {o['title']}: {_md_emph(o['text'], o.get('emphasis'))}" for o in lim["open_problems"]]
    return lines


EMPH_SHORT_WORDS = 40   # a text under this many words shows 1 emphasis phrase, a longer one 2 (hub_template.html emphCap)


def _md_emph(text: str, phrases) -> str:
    """Bold the emphasis phrases in Markdown as the hub's hl() marks them: the first whole-word occurrence of each,
    longest first, never overlapping, and at most 1 in a text under EMPH_SHORT_WORDS words, else 2."""
    w = lambda ch: ch.isascii() and (ch.isalnum() or ch == "_")
    spans: list[tuple[int, int]] = []
    cap = 1 if len(text.split()) < EMPH_SHORT_WORDS else 2
    for p in sorted((p for p in phrases or [] if isinstance(p, str) and p), key=len, reverse=True):
        if len(spans) >= cap:
            break
        i = text.find(p)
        while i >= 0:
            j = i + len(p)
            if not ((i > 0 and w(text[i - 1])) or (j < len(text) and w(text[j])) or any(i < b and j > a for a, b in spans)):
                spans.append((i, j))
                break
            i = text.find(p, i + 1)
    out, k = [], 0
    for a, b in sorted(spans):
        out += [text[k:a], "**", text[a:b], "**"]
        k = b
    return "".join(out) + text[k:]


# Fields the hub reads for a paper that is not included (Papers table, Coverage lookup, Decisions, Adjacent topics).
_HUB_SLIM_FIELDS = ("id", "ids", "title", "authors", "year", "type", "url", "status", "status_reason", "manual", "seed",
                    "seminal", "is_survey", "paper_role", "citations", "published_venue", "screening")


# The hub's Report tab is off for now (the user, 2026-10-26: "I don't want the report section right now (keep the spec
# and code for later stages - after we finish the video part)"). The review report is still made and merged: the
# video's executive summary, requirements, conditions, gaps and roadmap scenes draw on it.
HUB_REPORT_TAB = False


def hub_report(rv: Review) -> dict | None:
    """The hub's Report tab: the ten sections of an applied-research review (the user, 2026-10-26: "make sure that this
    information is also available in the hub itself"), from the review report (meta.report, which the page reads from
    meta) and the section tables computed here, the same ones the video draws."""
    papers = rv.tables["papers"]
    if not any(p.get("status") == "included" for p in papers.values()):
        return None
    ctx = _PresCtx(rv)
    val = lambda e: e.get("value") if isinstance(e, dict) else e
    fams = []
    for c in sorted(rv.tables["clusters"].values(), key=lambda c: c["id"]):
        x, w = ctx.X.get(c["id"]) or {}, c.get("winner_paper_id")
        fams.append({"id": c["id"], "name": c.get("name"), "definition": val(c.get("definition")),
                     "members": len(c.get("paper_ids") or []), "winner": w, "winner_short": _short_title(papers[w]) if w in papers else None,
                     "facets": {f: val(e) for f, e in (x.get("facets") or {}).items()}, "headline": x.get("headline"),
                     # the whole note (the video cuts it to fit a frame)
                     "dims": {f: n["value"] for f, n in _method_notes(papers[w]).items() if f in PRES_DIMENSIONS and n.get("value")}
                             if w in papers else {}})
    return {"sections": {str(n): list(v) for n, v in PRES_SECTIONS.items()}, "search": _pres_search(rv),
            "flow": _evidence_base(rv)["flow"], "summaries": ctx.summaries(), "families": fams,
            "provenance": _pres_provenance(rv, ctx), "weak_points": _pres_weak_points(rv, ctx),
            "feasibility": _pres_feasibility(rv, ctx), "labels": {"exec": {k: PRES_EXEC_LABELS[k] for k in _exec_parts(rv.meta.get("report"))},
                                                                  "support": REPORT_SUPPORT, "req": PRES_REQ_KEYS,
                                                                  "dims": PRES_DIMENSIONS, "gaps": REPORT_GAP_TYPES},
            "facets": FACETS}


def hub_paper(p: dict) -> dict:
    """A paper as the hub's Papers table shows it (the table lists every field as a column). An included paper keeps
    every field but the internal OpenAlex reference cache. A paper that is not included keeps only what the hub reads:
    evidence notes cut to value and status, the screening verdict, kind and area (status_reason carries the scope item
    and the reason), the first 3 authors. MDE's 2178 excluded abstracts pushed its hub to 16.77 MB; llm-to-domain-slm's
    6881 excluded papers were 13.4 MB of a 16.4 MB hub before any result row. data/ and the CSVs keep every field."""
    if p.get("status") == "included":
        return {f: v for f, v in p.items() if f != "_oa_work"}
    q = {f: p[f] for f in _HUB_SLIM_FIELDS if f in p}
    for f in ("citations", "published_venue"):
        if is_evidence(q.get(f)):
            q[f] = {"value": q[f]["value"], "status": q[f]["status"]}
    if isinstance(q.get("screening"), dict):
        q["screening"] = {k: q["screening"][k] for k in ("verdict", "kind", "area") if k in q["screening"]}
    if isinstance(q.get("authors"), list):
        q["authors"] = q["authors"][:3]
    if isinstance(q.get("ids"), dict):      # the hub reads only these two (Coverage lookup, BibTeX)
        q["ids"] = {k: q["ids"][k] for k in ("arxiv", "doi") if q["ids"].get(k)}
    for f in ("manual", "seed", "seminal", "is_survey"):
        if not q.get(f):
            q.pop(f, None)
    if q.get("paper_role") in (None, "method"):
        q.pop("paper_role", None)
    return q


def pipeline_url() -> str | None:
    """The pipeline-map link every rendered output carries: $LITREV_PIPELINE_URL if set (empty = no link), else the default."""
    v = os.environ.get("LITREV_PIPELINE_URL")
    return PIPELINE_URL_DEFAULT if v is None else (v.strip() or None)


_FORMULA_LEAD = ("=", "@", "\t", "\r")
_FORMULA_SIGNED = re.compile(r"[A-Za-z(!|=\"'&@:]")    # a +/- cell can call or reference something only with one of these


def _cell_text(s: str) -> str:
    """A CSV cell that a spreadsheet will not run as a formula (CWE-1236): text from outside sources (titles, abstracts)
    that starts with = @ tab or CR, or with + - and holds a letter or a formula sign, gets a leading '. Numbers keep
    their sign and marks ('-0.5', '-0.241*', '-12.3 ± 0.4') (security scan F7)."""
    if not isinstance(s, str) or not s:
        return s
    if s.startswith(_FORMULA_LEAD):
        return "'" + s
    if s[0] in "+-" and _FORMULA_SIGNED.search(s):
        try:
            float(s)
        except ValueError:
            return "'" + s
    return s


def render_review(rv: Review) -> dict:
    rv.derive()
    out_dir = rv.path / "rendered"
    (out_dir / "tables").mkdir(parents=True, exist_ok=True)
    (out_dir / "csv").mkdir(parents=True, exist_ok=True)
    result = {"tables": [], "csv": [], "xlsx": None, "hub": None, "review": None}
    purl = pipeline_url()
    built = f"How this review was built: [Litrev Pipeline Atlas]({purl})\n\n" if purl else ""
    sheets = {}
    for t in TABLES:
        cols, rows = _rows(rv, t)
        (out_dir / "tables" / f"{t}.md").write_text(f"# {t}\n\n" + built + (_md_table(cols, rows) if cols else "_empty_\n"))
        with (out_dir / "csv" / f"{t}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows([[_cell_text(x) for x in r] for r in rows])
        sheets[t] = (cols, rows)
        result["tables"].append(str(out_dir / "tables" / f"{t}.md"))
        result["csv"].append(str(out_dir / "csv" / f"{t}.csv"))
    try:
        import openpyxl
        from openpyxl.styles import Font
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for t, (cols, rows) in sheets.items():
            ws = wb.create_sheet(title=t[:31])
            ws.append(cols or ["(empty)"])
            for c in ws[1]:
                c.font = Font(bold=True)
            for i, r in enumerate(rows, start=2):
                vals = [x[:32000] for x in r]
                ws.append(vals)
                for j, x in enumerate(vals, start=1):
                    if isinstance(x, str) and x.startswith("="):   # openpyxl stores it as a live formula (F7)
                        ws.cell(row=i, column=j).data_type = "s"
            ws.freeze_panes = "A2"
        ws = wb.create_sheet(title="about")
        for k, v in (("review", rv.meta.get("slug")), ("purpose", rv.meta.get("purpose")), ("seed", rv.meta.get("seed")),
                     ("hub", rv.meta.get("hub_url")), ("pipeline map", purl)):
            ws.append([k, v or ""])
            ws.cell(ws.max_row, 1).font = Font(bold=True)
            if v and k in ("hub", "pipeline map"):
                ws.cell(ws.max_row, 2).hyperlink = v
        wb.save(out_dir / "review.xlsx")
        result["xlsx"] = str(out_dir / "review.xlsx")
    except ImportError:
        result["xlsx"] = None
    (out_dir / "review.bib").write_text((f"% How this review was built: {purl}\n\n" if purl else "") + bibtex(rv))
    result["bib"] = str(out_dir / "review.bib")
    # REVIEW.md
    errors, warnings = validate_review(rv)
    result["review"] = str(out_dir / "REVIEW.md")
    shown = Review(rv.path, tex_display(rv.meta), tex_display(rv.tables))   # formulas as text; never saved
    (out_dir / "REVIEW.md").write_text(_review_md(shown, tex_display(errors), tex_display(warnings), result))
    pres = load_presentation(rv) if (rv.meta.get("presentation") or {}).get("wanted") else {}
    if pres.get("plan"):          # step 8, only once the person opted in and a plan was merged
        (out_dir / "presentation").mkdir(exist_ok=True)
        audio = load_audio(rv)
        plan_md, script_md = _presentation_md(rv, tex_display(pres), audio)
        if audio.get("scenes"):     # what the hub needs: no word timings
            pres = dict(pres, audio={"total_s": audio.get("total_s"), "target_s": audio.get("target_s"), "voice": audio.get("voice"),
                                     "chapters": audio.get("chapters"), "flags": audio.get("flags"),
                                     "start_s": _audio_times(pres["plan"], pres.get("script") or {}, audio)})
        (out_dir / "presentation" / "PLAN.md").write_text(plan_md)
        (out_dir / "presentation" / "SCRIPT.md").write_text(script_md)
        result["presentation"] = [str(out_dir / "presentation" / n) for n in ("PLAN.md", "SCRIPT.md")]
    # hub
    tpl_path = Path(__file__).with_name("hub_template.html")
    if tpl_path.exists():
        rp = rank_papers(rv)
        # Result rows dominate the page (12.8k rows were 12.7 MB of a 17 MB hub, over the 16 MB artifact limit): drop
        # their null fields (the template reads a missing field and a null one alike) and write compact JSON.
        # papers and datasets that are not included and the discovery links repeat the same few values: packed like the results
        hp = {k: hub_paper(p) for k, p in rv.tables["papers"].items()}
        links = _pack_results({str(i): l for i, l in enumerate(rv.tables["discovery_links"])}, as_list=True)
        rview = hub_report(rv) if HUB_REPORT_TAB else None
        # The hub is written as pure ASCII: some viewers decode the page with the wrong charset, which turns
        # UTF-8 text into mojibake. Data uses JSON \u escapes; the template's script uses JS \u escapes.
        tpl = re.sub(r"[^\x00-\x7f]", lambda m: "\\u%04x" % ord(m.group()), tpl_path.read_text())
        title = _html.escape(rv.meta.get("slug", "review")).encode("ascii", "xmlcharrefreplace").decode()
        # Every text value shows its formulas as text (tex_display), the emphasis phrases with them, so hl() still finds them.
        fixed = tex_display({"meta": rv.meta, "ranking": rp, "figures": embed_explainer_figures(rv),
                             "result_tables": explainer_result_tables(rv, rp), "validation": {"errors": errors, "warnings": warnings},
                             "rendered_at": now_iso(), "coverage": load_coverage(rv), "pipeline_url": purl,
                             "presentation": pres if pres.get("plan") else None, "report_view": rview})
        other = tex_display({t: v for t, v in rv.tables.items() if t not in ("results", "papers", "datasets", "discovery_links")})
        other["discovery_links"] = tex_display(links)
        set_ranks = compute_set_ranks(rv)
        # One budget for the whole page, measured on the page itself (llm-to-domain-slm scope v2: only the result rows
        # had a budget, so the screened pool could grow the page past the limit with no error until publish). The result
        # rows get what is left; HUB_PAGE_TIERS run one by one while the page is still too big, and the first
        # HUB_TIERS_BEFORE_ROWS of them also run before result rows of included papers are left out, kept only if the
        # hub then shows more rows (llm-to-domain-slm: 6,594 cut reasons gave back no row, result tier 3 being one block).
        fits = []
        for level in range(len(HUB_PAGE_TIERS) + 1):
            papers, datasets, page_trim = hub_page_tables(rv, hp, level)
            tables = dict(other, results={}, papers=tex_display(papers), datasets=tex_display(datasets))
            room = HUB_LIMIT_BYTES - HUB_MARGIN_BYTES - len(_hub_html(tpl, title, dict(fixed, tables=tables, hub_trim=None, ranks={})))
            packed, trim = hub_results(rv, budget=min(RESULTS_HUB_BUDGET, room), per_row=HUB_RANK_BYTES_PER_ROW)
            trim.update(page_trim)
            tables["results"] = tex_display(packed)
            pk = packed["__packed__"]
            ci = pk["cols"].index("id") if "id" in pk["cols"] else None    # the ranks of rows the hub does not show are dropped too
            shown = set() if ci is None else {pk["strings"][r[ci]] if "id" in pk["str_cols"] else r[ci] for r in pk["rows"]}
            ranks = {k: list(v) for k, v in set_ranks.items() if k in shown}
            html_out = _hub_html(tpl, title, dict(fixed, tables=tables, hub_trim=trim, ranks=ranks))
            rows_cut = trim["rows_shown"] < trim["rows_total"] and room < RESULTS_HUB_BUDGET   # by the page, not the results cap
            if len(html_out) <= HUB_LIMIT_BYTES:
                fits.append((level, html_out, trim, tables))
                if not (rows_cut and level < HUB_TIERS_BEFORE_ROWS):
                    break
        if fits:                # the fewest page tiers that show the most rows
            level, html_out, trim, tables = min(fits, key=lambda f: (-f[2]["rows_shown"], f[0]))
        if trim["rows_shown"] < trim["rows_total"] or any(trim[k] for k in ("quotes_trimmed", *(n for n, _ in HUB_PAGE_TIERS))):
            result["hub_trim"] = trim
        (out_dir / "hub.html").write_text(html_out)
        result["hub"] = str(out_dir / "hub.html")
        result["hub_bytes"] = len(html_out)
        if len(html_out) > HUB_LIMIT_BYTES:      # every tier ran: say so at render (main exits 1), not at publish
            parts = dict({f"tables.{t}": v for t, v in tables.items()}, **fixed)
            big = sorted(((len(json.dumps(v, ensure_ascii=True, separators=(",", ":"))), k) for k, v in parts.items()), reverse=True)
            result["hub_over_limit"] = (
                f"hub.html is {len(html_out) / 1e6:.2f} MB, over the {HUB_LIMIT_BYTES / 1e6:.0f} MB artifact limit after every size "
                f"tier, so it cannot be published. Largest parts: {', '.join(f'{k} {n / 1e6:.1f} MB' for n, k in big[:4])}. "
                "Make the largest part smaller (see the Hub section of outputs.md), then render again.")
    return result


def _hub_html(tpl: str, title: str, payload: dict) -> str:
    """The hub page: the template with the payload inlined as pure-ASCII JSON. Every '<' is escaped: '<!--<script>' in a
    title would put the HTML parser in a state where </script> no longer ends the data element and the hub stays blank
    (security scan F8); \\u003c is the same character to JSON.parse."""
    blob = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    return tpl.replace("/*__DATA__*/", blob).replace("__TITLE__", title)


def _pack_results(results: dict, as_list: bool = False) -> dict:
    """Result rows for the hub as interned columns: {"__packed__": {cols, str_cols, strings, rows}}.
    Rows dominate the page (MDE at 92 papers: 21.8k rows, 14 MB of a 25 MB hub with nulls already dropped; 3.9 MB packed).
    String columns hold indexes into `strings`; a list-of-strings column (`strlist_cols`: author lists) a list of indexes;
    a column of dicts or lists that repeat (`val_cols`: venues, screening verdicts; at most half of them distinct) the
    index of the value in `values`; a column of other flat dicts (`objs`: ids, citation notes) [shape, v1, v2, ...], the
    shape's keys in `objs[col]["shapes"]` and the string values of `objs[col]["str_keys"]` interned. A dict keeps its key
    order and its null values (the template's isEv needs both value and status). Any other column keeps raw values
    (llm-to-domain-slm: the dict and list cells of 8,899 excluded papers were 2.1 MB, never interned). `values` holds
    objects, not JSON text, so tex_display reaches their strings as before. The template unpacks this back into
    {id: row} with null fields left out, which it reads like the unpacked table (_unpack_packed: the same in Python)."""
    cols = sorted({f for r in results.values() for f, v in r.items() if v is not None})
    flat = lambda d: all(x is None or isinstance(x, (str, int, float, bool)) for x in d.values())
    key = lambda v: json.dumps(v, separators=(",", ":"))
    str_cols, list_cols, val_cols, objs = [], [], [], {}
    for c in cols:
        vs = [r[c] for r in results.values() if r.get(c) is not None]
        if all(isinstance(v, str) for v in vs):
            str_cols.append(c)
        elif all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in vs):
            list_cols.append(c)
        elif all(isinstance(v, (dict, list)) for v in vs) and 2 * len({key(v) for v in vs}) <= len(vs):
            val_cols.append(c)
        elif all(isinstance(v, dict) and flat(v) for v in vs):
            keys = sorted({k for v in vs for k in v})
            objs[c] = {"shapes": [], "str_keys": [k for k in keys if all(isinstance(v[k], str) for v in vs if v.get(k) is not None)]}
    sc, lc, vc, sk = set(str_cols), set(list_cols), set(val_cols), {c: set(o["str_keys"]) for c, o in objs.items()}
    strings, idx, values, vidx, shapes = [], {}, [], {}, {}
    def s(v):
        if v not in idx:
            idx[v] = len(strings)
            strings.append(v)
        return idx[v]
    def enc(c, v):
        if v is None:
            return v
        if c in sc:
            return s(v)
        if c in lc:
            return [s(x) for x in v]
        if c in vc:
            if key(v) not in vidx:
                vidx[key(v)] = len(values)
                values.append(v)
            return vidx[key(v)]
        if c in objs:
            if (c, tuple(v)) not in shapes:
                shapes[(c, tuple(v))] = len(objs[c]["shapes"])
                objs[c]["shapes"].append(list(v))
            return [shapes[(c, tuple(v))]] + [s(x) if f in sk[c] and x is not None else x for f, x in v.items()]
        return v
    rows = [[enc(c, r.get(c)) for c in cols] for r in results.values()]
    out = {"cols": cols, "str_cols": str_cols, "strings": strings, "rows": rows}
    if list_cols:
        out["strlist_cols"] = list_cols
    if val_cols:
        out.update(val_cols=val_cols, values=values)
    if objs:
        out["objs"] = objs
    if as_list:
        out["list"] = True      # the template rebuilds a list in this order (discovery_links)
    return {"__packed__": out}


def _unpack_packed(tb: dict) -> dict | list:
    """A table written by _pack_results, rebuilt the way the hub template's unpacker does it (null fields left out;
    each row gets its own copy of a shared value)."""
    P = tb["__packed__"]
    S, sc, lc, ob = P["strings"], set(P["str_cols"]), set(P.get("strlist_cols") or []), P.get("objs") or {}
    vc = set(P.get("val_cols") or [])
    def dec(c, v):
        if c in sc:
            return S[v]
        if c in lc:
            return [S[j] for j in v]
        if c in vc:
            return json.loads(json.dumps(P["values"][v]))
        if c in ob:
            return {f: S[x] if x is not None and f in ob[c]["str_keys"] else x for f, x in zip(ob[c]["shapes"][v[0]], v[1:])}
        return v
    rows = [{c: dec(c, v) for c, v in zip(P["cols"], r) if v is not None} for r in P["rows"]]
    if P.get("list"):
        return rows
    out = {k: v for k, v in tb.items() if k != "__packed__"}
    out.update((o["id"], o) for o in rows)
    return out


# Packed result rows may take this much of the hub; past it, rows off the KPI boards are thinned in the hub only, tier by tier
# (llm-to-domain-slm: 46k rows were 11.4 MB packed without quotes, on a page limited to 16 MB).
RESULTS_HUB_BUDGET = 7_000_000
HUB_LIMIT_BYTES = 16_000_000      # the artifact page limit (claude.ai: 16 MB; the app refuses a hub over 16 MiB)
HUB_MARGIN_BYTES = 100_000        # the trim report and the ranks estimate; the rest of the page is measured
HUB_RANK_BYTES_PER_ROW = 16       # "R12345":[3,250], in the hub's ranks map
HUB_REASON_CHARS = 100            # tier "reasons_cut": the head of a status_reason (median 120 characters on llm-to-domain-slm)
HUB_SLIM_DROP = ("url", "authors", "published_venue", "citations")   # tier "papers_slimmed"


def _reason_head(s):
    """'screening: matches no in item: GraphGPT instruction-tunes an LLM with ...' -> its first HUB_REASON_CHARS
    characters, cut at a space, with '...': the verdict and the start of the reason."""
    if not isinstance(s, str) or len(s) <= HUB_REASON_CHARS:
        return s
    return s[:HUB_REASON_CHARS].rsplit(" ", 1)[0] + " ..."


def _tier_reasons_cut(rec: dict, is_paper: bool) -> dict:
    return dict(rec, status_reason=_reason_head(rec["status_reason"])) if _reason_head(rec.get("status_reason")) != rec.get("status_reason") else rec


def _tier_papers_slimmed(rec: dict, is_paper: bool) -> dict:
    return {f: v for f, v in rec.items() if f not in HUB_SLIM_DROP} if is_paper and any(f in rec for f in HUB_SLIM_DROP) else rec


# Page-size tiers for papers and datasets that are not included, in order, each only while the page is still over
# HUB_LIMIT_BYTES (the result rows get what is left of the page and are thinned by hub_results). The first
# HUB_TIERS_BEFORE_ROWS tiers also run before result rows of included papers are left out: a cut reason of an excluded
# paper costs a reader less than a result row. Every paper and dataset stays in the hub, so no link points nowhere.
HUB_PAGE_TIERS = [("reasons_cut", _tier_reasons_cut), ("papers_slimmed", _tier_papers_slimmed)]
HUB_TIERS_BEFORE_ROWS = 1


def hub_page_tables(rv: Review, hp: dict, level: int = 0) -> tuple[dict, dict, dict]:
    """The hub's papers and datasets tables with the first `level` HUB_PAGE_TIERS applied, and how many records each
    tier changed. `hp` is the papers table through hub_paper. Records that are not included are packed (_pack_results);
    included ones are never touched. data/ and the CSVs keep every field."""
    rep = {name: 0 for name, _ in HUB_PAGE_TIERS}
    def page(recs: dict, is_paper: bool) -> dict:
        out = {}
        for k, r in recs.items():
            for name, fn in HUB_PAGE_TIERS[:level]:
                new = fn(r, is_paper)
                rep[name] += new is not r
                r = new
            out[k] = r
        return out
    papers = {k: p for k, p in hp.items() if p.get("status") == "included"}
    papers.update(_pack_results(page({k: p for k, p in hp.items() if p.get("status") != "included"}, True)))
    D = rv.tables["datasets"]
    datasets = {k: d for k, d in D.items() if d.get("status") == "included"}
    datasets.update(_pack_results(page({k: d for k, d in D.items() if d.get("status") != "included"}, False)))
    return papers, datasets, rep


def _location_head(s: str | None) -> str | None:
    """'Table 3, row GPTQ, column Wiki2' -> 'Table 3': the part a reader needs to find the number in the paper."""
    return re.split(r"[,;(]", s)[0].strip()[:40] if s else s


def hub_results(rv: Review, budget: int = RESULTS_HUB_BUDGET, per_row: int = 0) -> tuple[dict, dict]:
    """The results table for the hub, packed, and a report of what the size budget took. Rows on a KPI board are never
    touched. Off-board rows are thinned in this order, each tier only while the page is still over the budget:
    1. the quote is left out; 2. the location keeps only its head ('Table 3'); 3. rows on a dataset or metric the review
    excluded (or with no dataset) are left out; 4. rows that re-report another paper's baseline are left out (a
    winner's comparison table is embedded apart, by explainer_result_tables). data/results.json, the CSV and the xlsx
    keep every row and field."""
    # rows of papers that are not included never reach the hub (llm-to-domain-slm scope v2: 19k rows of excluded papers)
    P = rv.tables["papers"]
    allr = rv.tables["results"]
    res = {k: r for k, r in allr.items() if (P.get(r.get("paper_id")) or {}).get("status") == "included"}
    # rows_total counts the included papers' rows only, so the hub's note (total - shown = the tiers below) adds up
    rep = {"rows_total": len(res), "rows_shown": len(res), "omitted_not_included": len(allr) - len(res),
           "quotes_trimmed": 0, "locations_cut": 0, "omitted_excluded_target": 0, "omitted_baseline": 0}
    packed = _pack_results(res)
    size = lambda x: len(json.dumps(x, separators=(",", ":"))) + per_row * len(x["__packed__"]["rows"])
    if size(packed) <= budget:
        return packed, rep
    keep = {r["id"] for k in rv.meta.get("kpis") or [] for r in _kpi_rows(rv, k)}
    D, M = rv.tables["datasets"], rv.tables["metrics"]
    excluded_target = lambda r: (not r.get("dataset_id") or (D.get(r["dataset_id"]) or {}).get("status") == "excluded"
                                 or (M.get(r.get("metric_id") or "") or {}).get("status") == "excluded")
    rows = dict(res)
    tiers = [("quotes_trimmed", lambda r: {f: v for f, v in r.items() if f != "quote"} if r.get("quote") else r),
             ("locations_cut", lambda r: dict(r, location=_location_head(r["location"])) if r.get("location") and _location_head(r["location"]) != r["location"] else r),
             ("omitted_excluded_target", lambda r: None if excluded_target(r) else r),
             ("omitted_baseline", lambda r: None if _is_baseline_row(r) else r)]
    for name, fn in tiers:
        nxt = {}
        for rid, r in rows.items():
            new = r if rid in keep else fn(r)
            if new is not r:
                rep[name] += 1
            if new is not None:
                nxt[rid] = new
        rows = nxt
        packed = _pack_results(rows)
        if size(packed) <= budget:
            break
    rep["rows_shown"] = len(rows)
    return packed, rep


def _review_md(rv: Review, errors: list[str], warnings: list[str], paths: dict) -> str:
    T = rv.tables
    papers = T["papers"]
    inc = [p for p in papers.values() if p["status"] == "included"]
    exc = [p for p in papers.values() if p["status"] == "excluded"]
    cand = [p for p in papers.values() if p["status"] == "candidate"]
    hist: dict[str, int] = {}
    for tbl in ("papers", "datasets", "tasks", "metrics", "benchmarks"):
        for e in T[tbl].values():
            for v in e.values():
                if is_evidence(v):
                    hist[v["status"]] = hist.get(v["status"], 0) + 1
    for r in T["results"].values():
        hist[r.get("status", "?")] = hist.get(r.get("status", "?"), 0) + 1
    lines = [f"# Literature review: {rv.meta.get('slug')}", "",
             f"*Last updated: {today()}* · purpose: {rv.meta.get('purpose')} · seed: {rv.meta.get('seed')}", ""]
    if rv.meta.get("hub_url"):
        lines += [f"Hub page: {rv.meta['hub_url']}", ""]
    if pipeline_url():
        lines += [f"How this review was built (steps, processes, code files): [Litrev Pipeline Atlas]({pipeline_url()})", ""]
    lines += ["## Status TL;DR", "",
              f"- Papers: {len(inc)} included, {len(cand)} candidate, {len(exc)} excluded (cap {rv.meta['caps'].get('max_papers')}, window {rv.meta['caps'].get('years')} y, seminal >= {rv.meta['caps'].get('seminal_citations')} citations)",
              f"- Search terms {len(T['search_terms'])} · search runs {len(T['search_runs'])} · discovery links {len(T['discovery_links'])}",
              f"- Tasks {len(T['tasks'])} · datasets {len(T['datasets'])} · benchmarks {len(T['benchmarks'])} · metrics {len(T['metrics'])} · results {len(T['results'])}",
              f"- Evidence status histogram: " + ", ".join(f"{k} {v}" for k, v in sorted(hist.items())) if hist else "- Evidence status histogram: (none yet)",
              f"- Validation: {len(errors)} errors, {len(warnings)} warnings", ""]
    if errors:
        lines += ["### Validation errors", ""] + [f"- {e}" for e in errors[:50]] + ([f"- ... {len(errors) - 50} more"] if len(errors) > 50 else []) + [""]
    if warnings:
        lines += ["### Warnings", ""] + [f"- {w}" for w in warnings[:50]] + ([f"- ... {len(warnings) - 50} more"] if len(warnings) > 50 else []) + [""]
    sc = rv.meta.get("scope")
    if sc:
        lines += ["## Scope", "", "- In: " + "; ".join(sc.get("in") or []), "- Out: " + ("; ".join(sc.get("out") or []) or "(none)"),
                  "- Protocol: " + ("; ".join(sc.get("protocol") or []) or "(none)"), f"- Agreed {sc.get('agreed_on')} by {sc.get('by')}"
                  + (f": {sc['notes']}" if sc.get("notes") else ""), ""]
    if rv.meta.get("decisions"):
        lines += ["## Decisions", ""] + [f"- {d['id']} {d['date']} [{d['kind']}, {d['by']}] {d['decision']} — {d['reason']}"
                                         for d in rv.meta["decisions"]] + [""]
    adj = rv.meta.get("adjacent")
    if adj:
        lines += ["## Adjacent topics (screened out)", "", f"*{adj['generated_at']}* · {len(adj.get('in_scope') or [])} on-topic papers "
                  f"excluded for access, window or cap; {len(adj.get('unrelated') or [])} unrelated papers not listed", ""]
        for t in adj.get("topics") or []:
            ps = sorted((papers[i] for i in t["paper_ids"] if i in papers and papers[i]["status"] == "excluded"),
                        key=lambda p: -(_cit(p) or 0))
            lines.append(f"- **{t['name']}** ({len(ps)}): {t.get('definition') or ''} Out because: {t.get('relation') or ''}")
            lines += [f"  - {p['id']} ({p.get('year')}) {p.get('title')} — {_cit(p)} cit." for p in ps[:3]]
        lines.append("")
    cov = tex_display(load_coverage(rv))
    if cov:
        lines += ["## Coverage", "", f"*{cov['generated_at']}* · {cov['neighbours_total']} works linked to >= {cov['min_links']} included "
                  f"papers, {len(cov['failed'])} failed edge lists", ""]
        for n in cov["neighbours"][:15]:
            st = f"{n['pool']['paper_id']} {n['pool']['status']}: {n['pool'].get('reason') or ''}" if n.get("pool") else "not in the pool"
            lines.append(f"- {n['title']} ({n.get('year')}, {n.get('citations')} cit.) — {n['links']} links — {st}")
        lines.append("")
    lines += ["## Included papers", ""]
    for p in sorted(inc, key=lambda p: (-(p.get("year") or 0), p["id"])):
        c = p["citations"].get("value") if is_evidence(p.get("citations")) else None
        lines.append(f"- {p['id']} ({p.get('year')}) {p.get('title')} — {c if c is not None else 'citations unverified'} cit. — {p.get('status_reason')}")
    lines += [""] + _step6_md(rv)
    lines += ["", "## Excluded papers (with reason)", ""]
    for p in sorted(exc, key=lambda p: p["id"]):
        lines.append(f"- {p['id']} ({p.get('year')}) {p.get('title')} — {p.get('status_reason')}")
    lines += ["", "## Search runs", ""]
    for s in T["search_runs"].values():
        lines.append(f"- {s['id']} [{s['kind']}/{s['source']}] {s['query']} — retrieved {s.get('retrieved')} — {s.get('executed_on')}")
    lines += ["", "## Files", "",
              "- `data/*.json` — source of truth (one file per table; stable IDs; evidence notes on facts)",
              "- `rendered/tables/*.md`, `rendered/csv/*.csv`, `rendered/review.xlsx` — regenerated by `litrev.py render`; never hand-edit",
              "- `rendered/hub.html` — single-file navigable page (publish as an Artifact)",
              "- `rendered/review.bib` — BibTeX for the included papers",
              "- `data/coverage.json` — what the review may be missing (`coverage`; derived, regenerate rather than edit)",
              "- `cache/` — HTTP cache and cached full texts (not committed)", "",
              "## Regenerate", "", "```", f"python3 {Path(__file__).resolve()} validate --review {rv.path}",
              f"python3 {Path(__file__).resolve()} render --review {rv.path}", "```", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# fetchers (network)
# --------------------------------------------------------------------------

def _oa(url: str) -> str:
    sep = "&" if "?" in url else "?"
    extra = []
    if MAILTO:
        extra.append("mailto=" + urllib.parse.quote(MAILTO))
    if OPENALEX_KEY:
        extra.append("api_key=" + urllib.parse.quote(OPENALEX_KEY))
    return url + (sep + "&".join(extra) if extra else "")


def _drop_credentials(url: str | None) -> str | None:
    """`url` without the mailto/api_key params _oa() adds. A stored URL reaches the CSV/MD tables and the published
    hub; every other param is kept byte for byte."""
    if not url or "?" not in url:
        return url
    base, _, query = url.partition("?")
    kept = [kv for kv in query.split("&") if kv.partition("=")[0] not in ("mailto", "api_key")]
    return base + ("?" + "&".join(kept) if kept else "")


OA_SELECT = "id,title,display_name,publication_year,cited_by_count,ids,doi,authorships,locations,primary_location,referenced_works,type,abstract_inverted_index"


def openalex_credits_remaining() -> int | None:
    h = LAST_HEADERS.get("api.openalex.org") or {}
    try:
        return int(h.get("x-ratelimit-remaining"))
    except (TypeError, ValueError):
        return None


def openalex_work_to_paper(w: dict) -> dict:
    authors = [a.get("author", {}).get("display_name") for a in w.get("authorships") or [] if a.get("author", {}).get("display_name")]
    oa_id = norm_openalex(w.get("id"))
    rec = {"ids": {"arxiv": openalex_arxiv_id(w), "doi": norm_doi((w.get("ids") or {}).get("doi") or w.get("doi")), "openalex": oa_id},
           "title": w.get("title") or w.get("display_name"), "authors": authors, "year": w.get("publication_year"),
           "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
           "type": w.get("type"), "abstract": openalex_abstract(w),
           "url": (w.get("primary_location") or {}).get("landing_page_url") or f"https://openalex.org/{oa_id}",
           "citations": evidence(w.get("cited_by_count"), "verified", source=f"https://api.openalex.org/works/{oa_id}", checked=today()),
           "sources": [f"https://api.openalex.org/works/{oa_id}"]}
    insts = openalex_institutions(w)
    if insts:
        rec["labs"] = evidence(insts, "verified", source=f"https://api.openalex.org/works/{oa_id}", checked=today(), location="authorships.institutions")
    pv = derive_published_venue({"authors": []}, [w])
    if pv:
        rec["published_venue"] = pv
    return rec


def openalex_resolve(rv: Review, p: dict, allow_search: bool = True) -> dict | None:
    """Resolve a paper to its best OpenAlex record. Cheap DOI filter first (arXiv DOI), then an
    exact-title search (10 credits) to find the published version with the real citation count."""
    cache = rv.cache_dir
    best = None
    seen_works: list[dict] = []   # every record of this paper looked at, for its published venue
    ax = p["ids"].get("arxiv")
    if ax:
        st, js = http_json(_oa(f"https://api.openalex.org/works?filter=doi:10.48550/arXiv.{ax}&select={OA_SELECT}"), cache)
        if js and js.get("results"):
            best = js["results"][0]
            seen_works.append(best)
    if p["ids"].get("doi") and not best:
        st, js = http_json(_oa(f"https://api.openalex.org/works?filter=doi:{urllib.parse.quote(p['ids']['doi'])}&select={OA_SELECT}"), cache)
        if js and js.get("results"):
            best = js["results"][0]
            seen_works.append(best)
    if p["ids"].get("openalex") and not best:
        st, js = http_json(_oa(f"https://api.openalex.org/works/{p['ids']['openalex']}?select={OA_SELECT}"), cache)
        if js:
            best = js
            seen_works.append(best)
    title = p.get("title")
    if allow_search and title:
        rem = openalex_credits_remaining()
        if rem is None or rem >= OPENALEX_MIN_CREDITS_FOR_SEARCH:
            q = urllib.parse.quote(re.sub(r"[^\w\s\-:]", " ", title)[:200])
            st, js = http_json(_oa(f"https://api.openalex.org/works?filter=title.search:{q}&select={OA_SELECT}&per-page=10"), cache, ttl=TTL_SEARCH)
            want = norm_title(title)
            seen_works += [r for r in (js or {}).get("results") or [] if norm_title(r.get("title") or r.get("display_name")) == want]
            pub = openalex_pick_best((js or {}).get("results") or [], title)
            if pub and (best is None or (pub.get("cited_by_count") or 0) > (best.get("cited_by_count") or 0)):
                if best and best.get("referenced_works") and not pub.get("referenced_works"):
                    pub["referenced_works"] = best["referenced_works"]
                if best and not pub.get("abstract_inverted_index"):
                    pub["abstract_inverted_index"] = best.get("abstract_inverted_index")
                best = pub
        elif not openalex_spent():   # spent: the note below says it once
            p.setdefault("sources", []).append("openalex title search skipped: credit budget low")
    if best:
        rec = openalex_work_to_paper(best)
        rec["ids"]["arxiv"] = rec["ids"]["arxiv"] or ax
        merged = rv.upsert_paper(rec)
        # citations: take the published record's count over a stored preprint count, but never lower a count: an
        # OpenAlex preprint record (Depth Anything 3: 2) must not overwrite S2's (748) and sink the paper in `select`
        cur = merged.get("citations") if is_evidence(merged.get("citations")) else None
        if cur is None or cur.get("value") is None or (rec["citations"].get("value") or 0) >= (cur.get("value") or 0):
            merged["citations"] = rec["citations"]
        merged["ids"]["openalex"] = rec["ids"]["openalex"]
        if rec.get("labs"):
            merged["labs"] = rec["labs"]
        merged["_oa_work"] = {"id": rec["ids"]["openalex"], "referenced_works": best.get("referenced_works") or []}
        _set_published_venue(merged, derive_published_venue(merged, [best] + seen_works, resolved=True))
        return best
    spent = openalex_spent()   # skipped, not absent: no "not found" venue; a later `paper` run resolves it
    if spent:
        note = "openalex skipped: daily credit budget used up; rerun `paper` after 00:00 UTC"
        if note not in p.setdefault("sources", []):
            p["sources"].append(note)
    _set_published_venue(p, derive_published_venue(p, seen_works, resolved=not spent))
    return None


def _set_published_venue(p: dict, pv: dict | None) -> None:
    """Recompute on every resolve, but a not-found never erases a venue an earlier record already showed."""
    cur = p.get("published_venue")
    if pv and (pv.get("status") in POSITIVE or not (is_evidence(cur) and cur.get("status") in POSITIVE)):
        p["published_venue"] = pv


def arxiv_query(rv: Review, search_query: str | None = None, id_list: list[str] | None = None, start: int = 0,
                max_results: int = 25, sort_by: str = "relevance", fresh: bool = False) -> tuple[str, list[dict]]:
    params = {"start": start, "max_results": max_results}
    if search_query:
        params["search_query"] = search_query
        params["sortBy"] = sort_by
        params["sortOrder"] = "descending"
    if id_list:
        params["id_list"] = ",".join(id_list)
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    st, body = http_get(url, rv.cache_dir, ttl=0 if fresh else TTL_SEARCH if search_query else TTL_FOREVER)
    if st != 200 or not body.strip().startswith("<"):
        return url, []
    return url, parse_arxiv_atom(body)


# Lucene's English stop set. arXiv's index drops these words, so an `all:<word>` clause for one matches nothing and the
# AND of a term's words returns 0 papers ("chain of thought distillation small models": 0, without "of": 81; measured
# word by word against arXiv on 2026-10-25: "from", "via", "using" and "over" are indexed).
ARXIV_STOPWORDS = frozenset("a an and are as at be but by for if in into is it no not of on or such that the their then "
                            "there these they this to was will with".split())
_ARXIV_STOP_CLAUSE = re.compile(r"\ball:(?:%s) AND |\s+AND all:(?:%s)$" % (("|".join(sorted(ARXIV_STOPWORDS)),) * 2), re.I)
# arXiv sometimes answers a search with a well-formed but empty feed (3 of 45 searches in a row on 2026-10-25 returned
# 0, and 164 / 24 / 91 results a few minutes later); an empty answer is fetched again, uncached, after these waits.
ARXIV_EMPTY_RETRY_WAITS = (10.0, 30.0)


def drop_arxiv_stopwords(search_query: str) -> str:
    """A stored word-AND search_query without its stop-word clauses (runs recorded before stop words were dropped)."""
    return _ARXIV_STOP_CLAUSE.sub("", search_query)


def arxiv_search_query(term: str, cats: list[str], years: int | None) -> str:
    """AND of the term's words over all fields (phrase-quoting a multi-word term collapses recall to
    near zero on arXiv), without the stop words arXiv does not index, restricted to the review's categories and the
    submitted-date window."""
    words = [w for w in re.split(r"[^\w\-]+", term) if w]
    words = [w for w in words if w.lower() not in ARXIV_STOPWORDS] or words
    sq = " AND ".join(f"all:{w}" for w in words) if words else f"all:{term}"
    if cats:
        sq += " AND (" + " OR ".join(f"cat:{c}" for c in cats) + ")"
    if years:
        start = _dt.date.today().year - int(years)
        sq += f" AND submittedDate:[{start}01010000 TO 209912312359]"
    return sq


def arxiv_entry_to_paper(e: dict) -> dict:
    return {"ids": {"arxiv": e["arxiv_id"], "doi": e.get("doi")}, "title": e["title"], "authors": e["authors"], "year": e["year"],
            "version": e.get("version"), "url": e["abs_url"], "abstract": e["abstract"], "categories": e["categories"],
            "venue": e.get("journal_ref") or None, "type": "preprint", "comment": e.get("comment") or None,
            "journal_ref": e.get("journal_ref") or None,
            "published_venue": derive_published_venue({"ids": {"arxiv": e["arxiv_id"]}, "comment": e.get("comment"),
                                                       "journal_ref": e.get("journal_ref"), "year": e["year"]}),
            "sources": [f"https://export.arxiv.org/api/query?id_list={e['arxiv_id']}"]}


def openalex_search_url(term: str, years: int | None, limit: int, now_year: int | None = None) -> str:
    """OpenAlex full-text search restricted to recent articles/preprints, ranked by relevance (not global
    citations, which surfaces SciPy/limma-style mega-cited papers for any query)."""
    now_year = now_year or _dt.date.today().year
    filters = ["type:article|preprint", "has_abstract:true"]
    if years:
        filters.append(f"publication_year:>{now_year - int(years) - 1}")
    return _oa(f"https://api.openalex.org/works?search={urllib.parse.quote(term)}&filter={','.join(filters)}"
               f"&select={OA_SELECT}&per-page={min(limit, 50)}&sort=relevance_score:desc")


def cmd_search(rv: Review, term: str, limit: int, engine: str, category: str | None, origin: str, rationale: str,
               sort_by: str = "relevance") -> dict:
    q = rv.add_term(term, category=category or "concept", origin=origin, rationale=rationale)
    cats = rv.meta.get("categories") or []
    if engine == "arxiv":
        sq = arxiv_search_query(term, cats, years=rv.meta["caps"].get("years"))
        url, entries = arxiv_query(rv, search_query=sq, max_results=limit, sort_by=sort_by)
        tries = 1
        for wait in ARXIV_EMPTY_RETRY_WAITS:
            if entries:
                break
            _sleep(wait)
            url, entries = arxiv_query(rv, search_query=sq, max_results=limit, sort_by=sort_by, fresh=True)
            tries += 1
        limits = "arXiv relevance ranking is not citation-aware; citation counts added via OpenAlex DOI filter"
        if not entries:
            limits += (f"; arXiv returned no results on {tries} tries (an empty feed can be a transient arXiv fault:"
                       " rerun the term later)")
        run = rv.add_run(kind="keyword", query=sq, term_ids=[q["id"]], source="arxiv", query_url=url,
                         scope=f"top {limit} by {sort_by}", retrieved=len(entries),
                         filters={"categories": cats, "sortBy": sort_by, "years": rv.meta["caps"].get("years")},
                         limitations=limits)
        for i, e in enumerate(entries):
            p = rv.upsert_paper(arxiv_entry_to_paper(e), source_run_id=run["id"])
            rv.add_link(run["id"], p["id"], "keyword", q["id"], "candidate", f"arXiv result #{i + 1} for {term!r}")
            if not p["ids"].get("openalex"):
                openalex_resolve(rv, p, allow_search=False)  # near-free DOI filter; full title search happens in `paper`
    elif engine == "openalex":
        url = openalex_search_url(term, rv.meta["caps"].get("years"), limit)
        st, js = http_json(url, rv.cache_dir, ttl=TTL_SEARCH)
        if st != 200:   # a failed search must not look like a searched term with 0 results (refresh would skip it)
            raise SystemExit(f"OpenAlex search failed (HTTP {st}"
                             + ("; the daily credit budget is used up, it resets at 00:00 UTC" if openalex_spent() else "")
                             + f"); nothing recorded for {term!r}. Rerun it later, or use --engine arxiv or s2.")
        works = (js or {}).get("results") or []
        run = rv.add_run(kind="keyword", query=f"search={term} (articles/preprints, last {rv.meta['caps'].get('years')} y)", term_ids=[q["id"]],
                         source="openalex", query_url=url, scope=f"top {limit} by relevance_score", retrieved=len(works),
                         filters={"years": rv.meta["caps"].get("years"), "type": "article|preprint"},
                         limitations="OpenAlex full-text search costs 10 credits; no arXiv-category filter exists")
        for i, w in enumerate(works):
            p = rv.upsert_paper(openalex_work_to_paper(w), source_run_id=run["id"])
            rv.add_link(run["id"], p["id"], "keyword", q["id"], "candidate", f"OpenAlex result #{i + 1} for {term!r}")
    elif engine == "s2":
        hdr = {"x-api-key": S2_KEY} if S2_KEY else {}
        url = ("https://api.semanticscholar.org/graph/v1/paper/search/bulk?query=" + urllib.parse.quote(term)
               + "&fields=title,year,externalIds,citationCount,authors,abstract,venue&sort=citationCount:desc")
        st, js = http_json(url, rv.cache_dir, headers=hdr, ttl=TTL_SEARCH)
        data = ((js or {}).get("data") or [])[:limit]
        run = rv.add_run(kind="keyword", query=f"bulk search={term}", term_ids=[q["id"]], source="semanticscholar", query_url=url,
                         scope=f"top {limit} by citationCount", retrieved=len(data),
                         limitations=None if st == 200 else f"HTTP {st} (unkeyed S2 is heavily rate limited)")
        for i, w in enumerate(data):
            p = rv.upsert_paper(s2_work_to_paper(w, url), source_run_id=run["id"])
            rv.add_link(run["id"], p["id"], "keyword", q["id"], "candidate", f"S2 result #{i + 1} for {term!r}")
    else:
        raise SystemExit(f"unknown engine {engine}")
    rv.derive()
    return {"term": q["id"], "run": run["id"], "retrieved": run["retrieved"]}


_PID_LIST = re.compile(r"^\s*P\d+(\s*[,\s]\s*P\d+)*\s*$")


def expand_ids(rv: Review, xs: list[str]) -> list[str]:
    """CLI paper arguments: `included`, or ids, where one argument may hold a comma/space-separated P-id list (a quoted
    shell variable arrives as one word). A P-id that is not in the store is an error, never a title to create."""
    if xs == ["included"]:
        return [p["id"] for p in rv.tables["papers"].values() if p["status"] == "included"]
    out = []
    for x in xs:
        out += re.split(r"[,\s]+", x.strip()) if _PID_LIST.match(x) else [x]
    unknown = [x for x in out if re.fullmatch(r"P\d+", x) and x not in rv.tables["papers"]]
    if unknown:
        raise SystemExit(f"unknown paper id(s): {', '.join(unknown)}")
    return out


def cmd_paper(rv: Review, ids: list[str], no_search: bool = False) -> list[str]:
    """Fetch metadata for arXiv ids / P-ids / DOIs; resolve OpenAlex citation layer."""
    out = []
    arx_batch, others = [], []
    for s in ids:
        if s in rv.tables["papers"]:
            p = rv.tables["papers"][s]
            if p["ids"].get("arxiv"):
                arx_batch.append(p["ids"]["arxiv"])
            else:
                others.append(p)
        elif norm_arxiv(s):
            arx_batch.append(norm_arxiv(s))
        else:
            others.append(rv.upsert_paper({"ids": {"doi": s} if s.startswith("10.") else {}, "title": None if s.startswith("10.") else s}))
    for i in range(0, len(arx_batch), 50):
        _, entries = arxiv_query(rv, id_list=arx_batch[i:i + 50], max_results=50)
        for e in entries:
            p = rv.upsert_paper(arxiv_entry_to_paper(e))
            others.append(p)
    seen = set()
    for p in others:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        openalex_resolve(rv, p, allow_search=not no_search)
        out.append(p["id"])
    rv.derive()
    return out


S2_FIELDS = "title,year,externalIds,citationCount,authors,abstract,venue"


def s2_work_to_paper(w: dict, url: str) -> dict:
    ext = w.get("externalIds") or {}
    return {"ids": {"arxiv": ext.get("ArXiv"), "doi": ext.get("DOI"), "s2": w.get("paperId")}, "title": w.get("title"),
            "authors": [a.get("name") for a in w.get("authors") or []], "year": w.get("year"), "venue": w.get("venue") or None,
            "abstract": w.get("abstract"), "url": f"https://www.semanticscholar.org/paper/{w.get('paperId')}",
            "citations": evidence(w.get("citationCount"), "verified", source=url, checked=today())
            if w.get("citationCount") is not None else evidence(None, "not-found", source=url, checked=today())}


def s2_paper_key(p: dict) -> str | None:
    ids = p.get("ids") or {}
    if ids.get("s2"):
        return ids["s2"]
    if ids.get("arxiv"):
        return f"arXiv:{ids['arxiv']}"
    if ids.get("doi") and not ids["doi"].lower().startswith("10.48550/"):
        return f"DOI:{ids['doi']}"
    return None


FORWARD_LIMIT = 100   # citers kept per forward walk (citations, snowball; also the S2 supplement's cap); one OpenAlex page
S2_EDGE_PAGES = 3   # citations: up to 3000 citing works (1000 per page); a highly cited seed lists more than one page


def _s2_edges(rv: Review, key: str, direction: str) -> tuple[list[dict], int, str]:
    """(works with a title, HTTP status of the first page, first-page URL) for an S2 /references or /citations list,
    most cited first. Citations are paged; references fit in one page."""
    edge, side = ("references", "citedPaper") if direction == "backward" else ("citations", "citingPaper")
    base = f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(key, safe=':')}/{edge}?fields={S2_FIELDS}&limit=1000"
    works, first_st = [], None
    for page in range(1 if direction == "backward" else S2_EDGE_PAGES):
        url = base + (f"&offset={page * 1000}" if page else "")
        st, js = http_json(url, rv.cache_dir, headers={"x-api-key": S2_KEY} if S2_KEY else {},
                           ttl=TTL_FOREVER if direction == "backward" else TTL_SEARCH)
        first_st = first_st or st
        data = (js or {}).get("data") or [] if st == 200 else []
        works += [d.get(side) or {} for d in data]
        if st != 200 or (js or {}).get("next") is None:
            break
    # entries without a paperId are S2's unmatched bibliography strings ("Estimation 23 Reconstruction"), not papers
    works = [w for w in works if w.get("title") and w.get("paperId")]
    works.sort(key=lambda w: -(w.get("citationCount") or 0))
    return works, first_st or 0, base


def _s2_walk(rv: Review, p: dict, direction: str, limit: int, why: str) -> dict | None:
    """Semantic Scholar fallback for a citation walk OpenAlex could not serve (`why`). Returns None when S2 has
    nothing either (no id, HTTP error, empty list), so the caller can try the next fallback. Works without a key
    (shared rate limit, HTTP 429 when busy); S2_API_KEY gives a dedicated one."""
    key = s2_paper_key(p)
    if not key:
        return None
    edge = "references" if direction == "backward" else "citations"
    works, st, url = _s2_edges(rv, key, direction)
    if not works:
        print(f"NOTE: {why}; Semantic Scholar fallback returned "
              f"{'nothing' if st == 200 else f'HTTP {st}'} for {key}", file=sys.stderr)
        return None
    kind = "reference" if direction == "backward" else "citing"
    run = rv.add_run(kind=kind, query=f"S2 {edge} of {key} ({len(works)} works) sorted by citationCount (top {limit})",
                     term_ids=[], source="semanticscholar", query_url=url, seed_paper_id=p["id"], direction=direction,
                     scope=f"top {limit}", retrieved=min(len(works), limit), filters={"fallback": "semanticscholar"},
                     limitations=f"{why}; walked Semantic Scholar instead (citation counts are S2's)")
    for i, w in enumerate(works[:limit]):
        np_ = rv.upsert_paper(s2_work_to_paper(w, url), source_run_id=run["id"])
        rv.add_link(run["id"], np_["id"], "survey" if p.get("is_survey") else kind, p["id"], "candidate",
                    f"{'cited by' if direction == 'backward' else 'cites'} {p['id']} (S2 #{i + 1})")
    print(f"NOTE: {why}; walked Semantic Scholar instead ({min(len(works), limit)} of {len(works)} works)", file=sys.stderr)
    rv.derive()
    return {"run": run["id"], "retrieved": min(len(works), limit), "source": "semanticscholar"}


_BIB_HEADING = re.compile(r"\n\s*(?:References|Bibliography|REFERENCES)\s*\n")
_BIB_ENTRY = re.compile(r"\n\[(\d+)\]\n\n([^\n]+)")   # arXiv-HTML extraction: "[n]", blank line, one-line entry
_BIB_AUTHORS_END = re.compile(r"(?:\bet al\.|\b[A-Za-z]{1,3})\.\s+(?=\S)")
_BIB_ARXIV = re.compile(r"(?:arXiv:|arxiv\.org/(?:abs|pdf)/)\s*(\d{4}\.\d{4,5})", re.I)
_TITLE_STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "via", "from", "by", "is", "at", "as"}


def parse_bibliography(text: str) -> list[dict]:
    """Entries of the last References section of a cached arXiv-HTML text: {n, raw, arxiv, year, title}.
    Other layouts (jina, pdftotext) are not parsed and return []; the caller records that as a limitation."""
    heads = list(_BIB_HEADING.finditer(text))
    if not heads:
        return []
    out = []
    for m in _BIB_ENTRY.finditer(text[heads[-1].start():]):
        raw = m.group(2).strip()
        years = re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", raw)
        ax = _BIB_ARXIV.search(raw)
        q = re.search(r"[\"“]([^\"”]{8,})[\"”]", raw)
        if q:
            title = q.group(1)
        else:
            a = _BIB_AUTHORS_END.search(raw)
            rest = raw[a.end():] if a else raw
            rest = re.sub(r"^\.\s*", "", rest)
            t = re.match(r"(.+?)(?:\.\s|\.$|,\s*(?:19|20)\d\d\.?$)", rest)
            title = (t.group(1) if t else rest).strip()
        title = re.sub(r",\s*(?:19|20)\d\d$", "", title).strip(" .,")
        out.append({"n": int(m.group(1)), "raw": raw, "arxiv": ax.group(1) if ax else None,
                    "year": int(years[-1]) if years else None, "title": title or None})
    return out


def _title_match(a: str | None, b: str | None) -> bool:
    import difflib
    x, y = norm_title(a), norm_title(b)
    return bool(x and y) and (x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.93)


def _bib_fallback(rv: Review, p: dict, no_search: bool, all_years: bool, oa: str | None) -> dict:
    """Backward walk from the bibliography of the paper's cached full text, for records whose OpenAlex
    referenced_works is empty (common for recent CVF-indexed and arXiv-only records). Entries are resolved free
    via arXiv (id_list, then a title search with a normalised-title match), then given OpenAlex citations
    (`openalex_resolve`; its 10-credit title search unless no_search). Entries outside the year window are skipped."""
    path = rv.fulltext_path(p["id"])
    why = f"OpenAlex record {oa} has no referenced_works" if oa else "paper could not be resolved in OpenAlex"
    if not path.exists():
        lim = f"{why}; no cached full text to parse - run `fulltext {p['id']}`, then `references {p['id']}` again"
        run = rv.add_run(kind="reference", query=f"backward of {p['id']}", term_ids=[], source="openalex", query_url=None,
                         seed_paper_id=p["id"], direction="backward", retrieved=0, limitations=lim)
        print(f"WARNING: {lim}", file=sys.stderr)
        return {"run": run["id"], "retrieved": 0, "warning": lim}
    entries = parse_bibliography(path.read_text())
    years = (rv.meta.get("caps") or {}).get("years")
    start = None if all_years or not years else _dt.date.today().year - int(years)
    window = [e for e in entries if start is None or e["year"] is None or e["year"] >= start]
    found: list[dict] = []
    unresolved: list[str] = []
    ax_ids = [e["arxiv"] for e in window if e["arxiv"]]
    by_ax = {}
    for i in range(0, len(ax_ids), 50):
        _, es = arxiv_query(rv, id_list=ax_ids[i:i + 50], max_results=50)
        by_ax.update({e["arxiv_id"]: e for e in es})
    for e in window:
        hit = by_ax.get(e["arxiv"]) if e["arxiv"] else None
        if hit is None and e["title"]:
            words = [w for w in re.split(r"[^\w]+", e["title"]) if len(w) > 1 and w.lower() not in _TITLE_STOP]
            if words:
                hit = arxiv_by_title(rv, e["title"])
        if hit is not None:
            np_ = rv.upsert_paper(arxiv_entry_to_paper(hit))
            openalex_resolve(rv, np_, allow_search=not no_search)
            found.append((e, np_))
            continue
        work = None
        if not no_search and e["title"]:
            rem = openalex_credits_remaining()
            if rem is None or rem >= OPENALEX_MIN_CREDITS_FOR_SEARCH:
                q = urllib.parse.quote(re.sub(r"[^\w\s\-:]", " ", e["title"])[:200])
                st, js = http_json(_oa(f"https://api.openalex.org/works?filter=title.search:{q}&select={OA_SELECT}&per-page=10"),
                                   rv.cache_dir, ttl=TTL_SEARCH)
                work = openalex_pick_best((js or {}).get("results") or [], e["title"])
        if work:
            found.append((e, rv.upsert_paper(openalex_work_to_paper(work))))
        else:
            unresolved.append(f"[{e['n']}] {e['title'] or e['raw'][:80]}")
    cites = lambda q: (q.get("citations") or {}).get("value") or 0 if is_evidence(q.get("citations")) else 0
    found.sort(key=lambda t: -cites(t[1]))
    lim = f"{why}; references parsed from the cached full text ({path.name})"
    if not entries:
        lim += "; no bibliography found (only the arXiv-HTML [n] layout is parsed)"
    if unresolved:
        lim += f"; {len(unresolved)} in-window entries unresolved: " + "; ".join(unresolved[:20]) + (" ..." if len(unresolved) > 20 else "")
    run = rv.add_run(kind="reference", query=f"bibliography of {p['id']} cached full text ({len(entries)} entries; "
                     f"{len(window)} in window; resolved {len(found)})", term_ids=[], source="arxiv", query_url=None,
                     seed_paper_id=p["id"], direction="backward", scope="every resolved in-window entry, by citations",
                     retrieved=len(found), filters={"from": "fulltext", **({"since_year": start} if start else {})},
                     limitations=lim)
    for i, (e, np_) in enumerate(found):   # all, not top `limit`: every resolved entry is already in the store
        np_.setdefault("discovery_run_ids", [])
        if run["id"] not in np_["discovery_run_ids"]:
            np_["discovery_run_ids"].append(run["id"])
        rv.add_link(run["id"], np_["id"], "survey" if p.get("is_survey") else "reference", p["id"], "candidate",
                    f"cited by {p['id']} as [{e['n']}] (#{i + 1})")
    print(f"NOTE: {why}; walked the bibliography of the cached full text instead "
          f"({len(found)} of {len(window)} in-window entries resolved)", file=sys.stderr)
    rv.derive()
    return {"run": run["id"], "retrieved": len(found), "entries": len(entries), "in_window": len(window), "unresolved": len(unresolved)}


def cmd_graph(rv: Review, pid: str, direction: str, limit: int, from_fulltext: bool = False, no_search: bool = False,
              all_years: bool = False) -> dict:
    p = rv.tables["papers"].get(pid) or (rv.find_paper({"arxiv": pid}) if norm_arxiv(pid) else None)
    if p is None:
        raise SystemExit(f"unknown paper {pid}; run `paper {pid}` first")
    if not p["ids"].get("openalex"):
        openalex_resolve(rv, p)
    oa = p["ids"].get("openalex")
    if direction == "backward" and from_fulltext:
        return _bib_fallback(rv, p, no_search, all_years, oa)
    spent = "OpenAlex daily credit budget used up"
    if not oa:
        r = _s2_walk(rv, p, direction, limit, spent if openalex_spent() else "paper could not be resolved in OpenAlex")
        if r:
            return r
    if direction == "backward" and not oa:
        return _bib_fallback(rv, p, no_search, all_years, None)
    if not oa:
        run = rv.add_run(kind="reference" if direction == "backward" else "citing", query=f"{direction} of {p['id']}", term_ids=[],
                         source="openalex", query_url=None, seed_paper_id=p["id"], direction=direction, retrieved=0,
                         limitations=f"{spent}; Semantic Scholar had nothing either" if openalex_spent()
                         else "paper could not be resolved in OpenAlex")
        return {"run": run["id"], "retrieved": 0}
    works = []
    if direction == "backward":
        refs = (p.get("_oa_work") or {}).get("referenced_works")
        if refs is None:
            st, js = http_json(_oa(f"https://api.openalex.org/works/{oa}?select=referenced_works"), rv.cache_dir)
            refs = (js or {}).get("referenced_works") or []
        if not refs:
            return _s2_walk(rv, p, direction, limit, spent if openalex_spent() else f"OpenAlex record {oa} has no referenced_works") \
                or _bib_fallback(rv, p, no_search, all_years, oa)
        # Fetch every reference (OpenAlex id filters cost no search credits), then keep the most cited in the year
        # window. referenced_works is listed oldest-first, so cutting it at --limit before sorting kept only 1998-2019
        # papers from the dashcam survey and lost TransGeo (2022) and L2LTR (2021).
        ids = [norm_openalex(r) for r in refs]
        url = None
        limitations = None
        for i in range(0, len(ids), 50):
            url = _oa(f"https://api.openalex.org/works?filter=openalex:{'|'.join(ids[i:i + 50])}&select={OA_SELECT}&per-page=50")
            st, js = http_json(url, rv.cache_dir)
            works += (js or {}).get("results") or []
        caps = rv.meta["caps"]
        min_year = _dt.date.today().year - int(caps.get("years", 3))
        seminal = int(caps.get("seminal_citations", 1000)) if int(caps.get("max_seminal", 10)) > 0 else None
        cites = lambda w: -(w.get("cited_by_count") or 0)
        inwin = sorted((w for w in works if all_years or (w.get("publication_year") or 0) >= min_year), key=cites)
        classic = sorted((w for w in works if not all_years and (w.get("publication_year") or 0) < min_year
                          and seminal is not None and (w.get("cited_by_count") or 0) >= seminal), key=cites)
        works = inwin + classic   # in-window first; pre-window classics only fill the slots left
        kind, dtype = "reference", "reference"
        query = (f"referenced_works of {oa} ({len(refs)} refs; {len(inwin)} in window"
                 + (f" + {len(classic)} pre-window >= {seminal} citations" if classic else "") + f"; kept top {limit} by citations)")
    if direction != "backward":
        limitations = None
    if direction != "backward":
        url = _oa(f"https://api.openalex.org/works?filter=cites:{oa}&sort=cited_by_count:desc&select={OA_SELECT}&per-page={min(limit, 200)}")
        st, js = http_json(url, rv.cache_dir, ttl=TTL_SEARCH)
        works = (js or {}).get("results") or []
        if not works:
            r = _s2_walk(rv, p, direction, limit, f"OpenAlex lists no work citing {oa}" if st == 200
                         else spent if openalex_spent() else f"OpenAlex HTTP {st}")
            if r:
                return r
        kind, dtype = "citing", "citing"
        query = f"filter=cites:{oa} sorted by cited_by_count (top {limit})"
    recs = [(openalex_work_to_paper(w), f"#{i + 1}") for i, w in enumerate(works[:limit])]
    filters = None
    if direction != "backward" and s2_paper_key(p):
        # OpenAlex misses every citing paper whose own record has no referenced_works (Depth Anything 3 for Depth
        # Anything V2), so forward walks always add Semantic Scholar's most-cited citing works it did not list.
        s2w, s2st, s2url = _s2_edges(rv, s2_paper_key(p), "forward")
        seen = {k for r, _ in recs for k in _rec_keys(r)}
        extra = [s2_work_to_paper(w, s2url) for w in s2w]
        extra = [r for r in extra if not (_rec_keys(r) & seen)][:limit]
        recs += [(r, f"S2 #{i + 1}") for i, r in enumerate(extra)]
        filters = {"supplement": "semanticscholar", "s2_added": len(extra)}
        query += f" + S2 citations not in OpenAlex (top {limit}, {len(extra)} added)"
        if s2st != 200:
            limitations = f"Semantic Scholar supplement failed (HTTP {s2st}); OpenAlex only - rerun later"
            filters["s2_failed"] = s2st   # machine-readable: `snowball` re-walks these first, `status` lists them
            print(f"WARNING: {limitations}", file=sys.stderr)
    run = rv.add_run(kind=kind, query=query, term_ids=[], source="openalex", query_url=url, seed_paper_id=p["id"],
                     direction=direction, scope=f"top {limit}", retrieved=len(recs), limitations=limitations, filters=filters)
    for rec, tag in recs:
        np_ = rv.upsert_paper(rec, source_run_id=run["id"])
        rv.add_link(run["id"], np_["id"], "survey" if p.get("is_survey") else dtype, p["id"], "candidate",
                    f"{'cited by' if direction == 'backward' else 'cites'} {p['id']} ({tag})")
    rv.derive()
    return {"run": run["id"], "retrieved": len(recs), **({"s2_added": filters["s2_added"]} if filters else {})}


def _rec_keys(r: dict) -> set:
    ids = r.get("ids") or {}
    return {k for k in (("arxiv", norm_arxiv(ids.get("arxiv"))), ("doi", norm_doi(ids.get("doi"))),
                        ("title", norm_title(r.get("title")) or None)) if k[1]}


WALK_KINDS = ("reference", "citing", "refresh-citing", "coverage")   # coverage-found papers are hop 1, like survey refs


def snowball_frontier(rv: Review, max_hop: int = 10) -> tuple[list[str], list[str], dict[str, int]]:
    """(frontier, too_deep, found_at) for the next `snowball` hop: included walk-found papers not yet forward-walked. Pure."""
    runs = rv.tables["search_runs"]
    run_hop = {sid: (s.get("filters") or {}).get("hop", 1) for sid, s in runs.items() if s["kind"] in WALK_KINDS}
    walked = {s["seed_paper_id"] for s in runs.values() if s["kind"] in ("citing", "refresh-citing")}
    found_at: dict[str, int] = {}
    for l in rv.tables["discovery_links"]:
        if l["run_id"] in run_hop:
            found_at[l["paper_id"]] = min(found_at.get(l["paper_id"], 99), run_hop[l["run_id"]])
    papers = rv.tables["papers"]
    frontier = sorted(pid for pid in found_at if pid in papers and papers[pid]["status"] == "included" and pid not in walked)
    too_deep = [pid for pid in frontier if found_at[pid] + 1 > max_hop]
    return [pid for pid in frontier if pid not in too_deep], too_deep, found_at


def _id_num(i: str) -> int:
    """S0099 -> 99 (ids grow past their zero padding: S1000 sorts before S999 as text)."""
    m = re.search(r"\d+", i or "")
    return int(m.group()) if m else -1


def s2_failed_walks(rv: Review) -> list[str]:
    """Forward walks whose Semantic Scholar supplement failed (HTTP 429 on the unkeyed pool) and that no later walk from
    the same included seed has redone. table-qa 2026-10-29: 75 walks lost their S2 citers and were rerun by hand."""
    runs, papers = rv.tables["search_runs"], rv.tables["papers"]
    out = []
    for sid, s in runs.items():
        f = s.get("filters") or {}
        failed = f.get("s2_failed") or "Semantic Scholar supplement failed" in str(s.get("limitations") or "")   # pre-1.36 runs
        if s["kind"] != "citing" or not failed or f.get("s2_retried"):
            continue
        if (papers.get(s.get("seed_paper_id") or "") or {}).get("status") != "included":
            continue
        later_ok = any(t["kind"] == "citing" and t.get("seed_paper_id") == s["seed_paper_id"] and _id_num(tid) > _id_num(sid)
                       and not (t.get("filters") or {}).get("s2_failed")
                       and "Semantic Scholar supplement failed" not in str(t.get("limitations") or "") for tid, t in runs.items())
        if not later_ok:
            out.append(sid)
    return sorted(out, key=_id_num)


def cmd_snowball(rv: Review, limit: int = FORWARD_LIMIT, max_hop: int = 10) -> dict:
    """One more hop of forward walks: walk `citations` from every included paper that a walk found and that has
    not been forward-walked itself. A walk from a seed/survey is hop 1 (no `filters.hop`); a walk from a paper first
    found at hop h is hop h+1, recorded in the run's `filters.hop`. `select` and screening happen between calls, so
    only screened-in papers grow the frontier. Converged when the frontier is empty or every node would exceed max_hop."""
    runs, papers = rv.tables["search_runs"], rv.tables["papers"]
    before = set(papers)
    # first redo walks whose S2 supplement failed (each once per call; a walk that fails again stays listed)
    rewalked, still_failed = [], []
    for sid in s2_failed_walks(rv):
        old = runs[sid]
        r = cmd_graph(rv, old["seed_paper_id"], "forward", limit)
        hop = (old.get("filters") or {}).get("hop")
        runs[r["run"]]["filters"] = {**(runs[r["run"]].get("filters") or {}), **({"hop": hop} if hop else {})}
        old["filters"] = {**(old.get("filters") or {}), "s2_retried": r["run"]}
        (still_failed if (runs[r["run"]].get("filters") or {}).get("s2_failed") else rewalked).append(r["run"])
        rv.save()
    retry = {"s2_rewalked": rewalked, "s2_still_failed": still_failed} if rewalked or still_failed else {}
    frontier, too_deep, found_at = snowball_frontier(rv, max_hop)
    if not frontier:
        reason = f"max hop {max_hop} reached for {', '.join(too_deep)}" if too_deep else "no included walk-found paper left to walk"
        new = sorted(set(papers) - before)
        return {"hop": None, "frontier": [], "runs": [], "new_papers": len(new), **({"new_paper_ids": new} if new else {}),
                "converged": not new, "reason": reason + ("; S2 re-walks added papers - screen them" if new else ""), **retry}
    out = []
    for pid in frontier:
        r = cmd_graph(rv, pid, "forward", limit)
        runs[r["run"]]["filters"] = {**(runs[r["run"]].get("filters") or {}), "hop": found_at[pid] + 1}
        out.append(r["run"])
        rv.save()   # per paper: a hop killed by a tool timeout keeps its walks, and a rerun walks only the rest
    new = sorted(set(papers) - before)
    return {"hop": max(found_at[pid] + 1 for pid in frontier), "frontier": frontier, "runs": out,
            "new_papers": len(new), "new_paper_ids": new, "converged": False,
            "skipped_max_hop": too_deep, **retry}


# --------------------------------------------------------------------------
# refresh (alert-only living reviews)
# --------------------------------------------------------------------------

_ARXIV_DATE = re.compile(r"\s*AND\s+submittedDate:\[[^\]]*\]")
# Windows overlap the previous run: arXiv lists a paper only once it is announced (up to ~3 days after submission,
# more over weekends, and dates are UTC); OpenAlex indexes works days to weeks after their publication_date.
# Papers already in the store are deduped, so the overlap costs requests, not duplicates.
REFRESH_OVERLAP_DAYS = {"arxiv": 5, "openalex": 30}


def _next_day(d: str) -> str:
    return (_dt.date.fromisoformat(d) + _dt.timedelta(days=1)).isoformat()


def _arxiv_query_since(query: str, since: str | None) -> str:
    """The stored arXiv search_query with its submittedDate window replaced by [since 00:00, open end]."""
    if since is None:
        return query
    return f"{_ARXIV_DATE.sub('', query)} AND submittedDate:[{since.replace('-', '')}0000 TO 209912312359]"


def _openalex_url_since(url: str, since: str, limit: int) -> str:
    """A stored OpenAlex works URL restricted to from_publication_date, newest first, `limit` per page, and
    without mailto/api_key (those are added by _oa at request time and must never be stored)."""
    u = urllib.parse.urlsplit(url)
    out, has_filter = [], False
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        if k in ("mailto", "api_key", "sort", "per-page"):
            continue
        if k == "filter":
            has_filter = True
            v = ",".join([f for f in v.split(",") if f and not f.startswith("from_publication_date:")] + [f"from_publication_date:{since}"])
        out.append((k, v))
    if not has_filter:
        out.append(("filter", f"from_publication_date:{since}"))
    out += [("sort", "publication_date:desc"), ("per-page", str(min(limit, 200)))]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(out, safe=":,|"), ""))


def _scope_limit(s: dict) -> int:
    m = re.search(r"top (\d+)", s.get("scope") or "")
    return max(15, int(m.group(1))) if m else 15


def refresh_plan(rv: Review, terms: list[str] | None = None, limit: int | None = None) -> dict:
    """Which replays a refresh would run (pure; no network). Keyword terms with status `searched` (or the
    `terms` named) and every citing walk; reference walks never (a reference list does not grow)."""
    runs = sorted(rv.tables["search_runs"].values(), key=lambda s: s["id"])
    ok = lambda s: not (s.get("filters") or {}).get("refresh_failed")
    want, now = set(terms or []), today()
    unknown = sorted(want - set(rv.tables["search_terms"]))
    if unknown:
        raise SystemExit(f"unknown term id(s): {', '.join(unknown)} (see `list --table search_terms`)")
    replays, skipped = [], []

    def last_of(mine: list[dict]) -> str | None:
        done = [s["executed_on"] for s in mine if ok(s) and s.get("executed_on")]
        return max(done) if done else None

    def since_from(last: str | None, source: str) -> str | None:
        if last is None:
            return None
        return (_dt.date.fromisoformat(last) - _dt.timedelta(days=REFRESH_OVERLAP_DAYS[source])).isoformat()

    for q in sorted(rv.tables["search_terms"].values(), key=lambda q: q["id"]):
        if want and q["id"] not in want:
            continue
        if not want and q.get("status") != "searched":
            skipped.append({"what": q["id"], "reason": f"term status {q.get('status')}"})
            continue
        mine = [s for s in runs if q["id"] in (s.get("term_ids") or []) and s["kind"] in ("keyword", "refresh-keyword")]
        tmpl = next((s for s in reversed(mine) if s["kind"] == "keyword"), None)
        last = last_of(mine)
        if last is not None and last >= now:
            skipped.append({"what": q["id"], "reason": "already searched today"})
            continue
        since = since_from(last, "openalex" if tmpl and tmpl.get("source") == "openalex" else "arxiv")
        n = limit or (_scope_limit(tmpl) if tmpl else 15)
        base = {"kind": "refresh-keyword", "source_run_id": tmpl["id"] if tmpl else None, "term_id": q["id"],
                "seed_paper_id": None, "since": since, "limit": n, "label": q["term"]}
        if tmpl is None or tmpl.get("source") == "arxiv":
            sq = (drop_arxiv_stopwords(tmpl["query"]) if tmpl
                  else arxiv_search_query(q["term"], rv.meta.get("categories") or [], rv.meta["caps"].get("years")))
            replays.append({**base, "source": "arxiv", "query": _arxiv_query_since(sq, since), "url": None})
        elif tmpl.get("source") == "openalex" and tmpl.get("query_url") and since:
            replays.append({**base, "source": "openalex", "query": None, "url": _openalex_url_since(tmpl["query_url"], since, n)})
        else:
            skipped.append({"what": q["id"], "reason": f"source {tmpl.get('source')} not supported by refresh"})

    walks: dict[str, list[dict]] = {}
    for s in runs:
        if s["kind"] in ("citing", "refresh-citing") and s.get("seed_paper_id"):
            walks.setdefault(s["seed_paper_id"], []).append(s)
    for pid, mine in sorted(walks.items()):
        if want:
            skipped.append({"what": f"citing walk of {pid}", "reason": "--terms given: citing walks not replayed"})
            continue
        tmpl = next((s for s in reversed(mine) if s["kind"] == "citing" and s.get("query_url") and s.get("source") == "openalex"), None)
        if tmpl is None:
            s2 = any(s.get("source") == "semanticscholar" for s in mine)
            skipped.append({"what": f"citing walk of {pid}", "reason": "Semantic Scholar fallback walk: not replayed; rerun "
                            f"`citations {pid}` to refresh it" if s2 else "seed not resolved in OpenAlex"})
            continue
        last = last_of(mine)
        if last is not None and last >= now:
            skipped.append({"what": f"citing walk of {pid}", "reason": "already searched today"})
            continue
        since = since_from(last, "openalex") or now
        n = limit or _scope_limit(tmpl)
        replays.append({"kind": "refresh-citing", "source": "openalex", "source_run_id": tmpl["id"], "term_id": None,
                        "seed_paper_id": pid, "since": since, "limit": n, "query": None,
                        "url": _openalex_url_since(tmpl["query_url"], since, n), "label": f"papers citing {pid}"})
    return {"replays": replays, "skipped": skipped}


def _find_existing(rv: Review, rec: dict) -> dict | None:
    ids = rec.get("ids") or {}
    return rv.find_paper({"arxiv": norm_arxiv(ids.get("arxiv")), "doi": norm_doi(ids.get("doi")),
                          "openalex": norm_openalex(ids.get("openalex")), "s2": ids.get("s2")}, rec.get("title"))


def write_refresh_report(rv: Review, res: dict) -> Path:
    out = rv.path / "rendered"
    out.mkdir(parents=True, exist_ok=True)
    f = out / "REFRESH.md"
    if f.exists():
        prev = (rv.meta.get("last_refresh") or {}).get("date") or "previous"
        dst, i = out / f"REFRESH-{prev}.md", 2
        while dst.exists():
            dst, i = out / f"REFRESH-{prev}-{i}.md", i + 1
        f.replace(dst)
    script = Path(__file__).resolve()
    since = sorted({(rv.tables["search_runs"][r].get("filters") or {}).get("since") for r in res["replays"]} - {None})
    window = f"Windows start {since[0]} to {since[-1]} (each replay looks only at papers newer than its last run)." if since else ""
    L = [f"# Refresh {res['date']} - {rv.meta.get('slug')}", "", window, "",
         *([f"How this review was built: [Litrev Pipeline Atlas]({pipeline_url()})", ""] if pipeline_url() else []),
         f"{len(res['replays'])} replays run ({len(res['failed'])} failed), {len(res['skipped'])} skipped, "
         f"{len(res['new'])} new candidate{'s' if len(res['new']) != 1 else ''}. Nothing was screened, ranked or published.", ""]
    if not res["new"]:
        L += ["Nothing new.", ""]
    else:
        L += ["## New candidates", "",
              "Include one with the line under it, then run `paper`, `fulltext`, `enrich` and step 5 for it, and re-run "
              "step 6 before the hub reflects it.", ""]
        for n in tex_display(res["new"]):              # titles and abstracts with their formulas as text
            L += [f"### {n['paper_id']} - {n['title']} ({n['year'] or 'year unknown'})", "",
                  f"- Link: {n['url']}", f"- Found by: {', '.join(n['paths']) or 'no recorded path'}"]
            if n.get("abstract"):
                L.append(f"- Abstract: {n['abstract'][:300]}{'...' if len(n['abstract']) > 300 else ''}")
            L += ["", "```", f"python3 {script} screen --review {rv.path} --id {n['paper_id']} --decision included --reason \"<why>\"", "```", ""]
    if res.get("capped"):
        L += ["## Possibly incomplete", "", "These replays returned their full cap, so older papers in the window may be missing. "
              "Re-run with a higher `--limit` (and `--terms` for the keyword ones) to see them.", ""] + \
             [f"- {c['what']}: cap {c['limit']} ({c['run_id']})" for c in res["capped"]] + [""]
    if res["failed"]:
        L += ["## Failed replays (retried next time)", ""] + [f"- {x['what']}: HTTP {x['status']} ({x['run_id']})" for x in res["failed"]] + [""]
    if res["skipped"]:
        L += ["## Skipped", ""] + [f"- {s['what']}: {s['reason']}" for s in res["skipped"]] + [""]
    f.write_text("\n".join(L))
    return f


def cmd_refresh(rv: Review, terms: list[str] | None = None, limit: int | None = None, dry_run: bool = False) -> dict:
    """Alert-only refresh: replay the plan, store new papers as unscreened candidates, write rendered/REFRESH.md.
    Never calls select and never changes an existing paper's status."""
    plan = refresh_plan(rv, terms, limit)
    if dry_run:
        return {"dry_run": True, **plan, "requests": len(plan["replays"])}
    date, before = today(), set(rv.tables["papers"])
    res = {"date": date, "replays": [], "skipped": plan["skipped"], "failed": [], "capped": [], "new": []}
    paths: dict[str, list[str]] = {}
    for r in plan["replays"]:
        if r["source"] == "arxiv":
            url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
                {"start": 0, "max_results": r["limit"], "search_query": r["query"], "sortBy": "submittedDate", "sortOrder": "descending"})
            st, body = http_get(url, rv.cache_dir, ttl=0)   # never served from cache: a cached failure would block the retry
            good = st == 200 and body.strip().startswith("<")
            recs = [arxiv_entry_to_paper(e) for e in parse_arxiv_atom(body)] if good else []
            query, stored = r["query"], url
        else:
            st, js = http_json(_oa(r["url"]), rv.cache_dir, ttl=0)
            good = st == 200 and js is not None
            recs = [openalex_work_to_paper(w) for w in ((js or {}).get("results") or [])[: r["limit"]]]
            query, stored = f"{r['label']} published since {r['since']}", r["url"]
        filters = {"since": r["since"], "sortBy": "submittedDate" if r["source"] == "arxiv" else "publication_date:desc"}
        if not good:
            filters["refresh_failed"] = True
        citing = r["kind"] == "refresh-citing"
        hop = (rv.tables["search_runs"].get(r["source_run_id"], {}).get("filters") or {}).get("hop")
        if citing and hop:
            filters["hop"] = hop                       # a replay of a snowball walk keeps its hop for `snowball`
        run = rv.add_run(kind=r["kind"], query=query, term_ids=[r["term_id"]] if r["term_id"] else [], source=r["source"],
                         query_url=stored, scope=f"top {r['limit']} newest" + (f" since {r['since']}" if r["since"] else ""),
                         retrieved=len(recs), seed_paper_id=r["seed_paper_id"], direction="forward" if citing else None,
                         filters=filters,
                         limitations=None if good else f"HTTP {st}: replay failed; this window is retried on the next refresh")
        run["source_run_id"] = r["source_run_id"]
        res["replays"].append(run["id"])
        if good and len(recs) >= r["limit"]:
            run["limitations"] = (f"returned its cap ({r['limit']}); older papers in the window may be missing - "
                                  f"re-run with a higher --limit")
            res["capped"].append({"run_id": run["id"], "what": r["term_id"] or f"citing walk of {r['seed_paper_id']}", "limit": r["limit"]})
        if not good:
            res["failed"].append({"run_id": run["id"], "what": r["term_id"] or f"citing walk of {r['seed_paper_id']}", "status": st})
            continue
        origin = r["term_id"] or r["seed_paper_id"]
        for i, rec in enumerate(recs):
            # an existing paper is only linked, never merged into: a refresh must not change citations or other
            # fields that rank_papers / cluster winners read
            p = _find_existing(rv, rec) or rv.upsert_paper(rec, source_run_id=run["id"])
            rv.add_link(run["id"], p["id"], "citing" if citing else "keyword", origin, p["status"],
                        f"refresh {date}: result #{i + 1} for {r['label']!r}")
            tag = f"{'citing walk of' if citing else 'term'} {origin}"
            if tag not in paths.setdefault(p["id"], []):
                paths[p["id"]].append(tag)
    P = rv.tables["papers"]
    for pid in [pid for pid in P if pid not in before]:
        p, n = P[pid], len(paths.get(pid, []))
        p["status"] = "candidate"
        p["status_reason"] = f"refresh {date}: found by {n} path{'s' if n != 1 else ''}; not screened"
        p["discovered_by_refresh"] = date
        res["new"].append({"paper_id": pid, "title": p.get("title"), "year": p.get("year"), "url": p.get("url"),
                           "abstract": p.get("abstract"), "paths": paths.get(pid, [])})
    res["new"].sort(key=lambda n: (-len(n["paths"]), -(n["year"] or 0), n["paper_id"]))
    rv.derive()
    res["report"] = str(write_refresh_report(rv, res))
    rv.meta["refreshed_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rv.meta["last_refresh"] = {"date": date, "replays": len(res["replays"]), "failed": len(res["failed"]),
                               "new": len(res["new"]), "report": "rendered/REFRESH.md"}
    return res


def oa_locations(rv: Review, p: dict) -> tuple[list[str], list[str]]:
    """Open-access PDF URLs and OA landing pages from the paper's OpenAlex record (a filter-free lookup by id, or by
    DOI when the record has no OpenAlex id; no credits). PDFs: repository copies first (institutional eprints rarely
    sit behind bot checks), then publisher PDFs. Landing pages are only those of OA locations (MDPI, IEEE and Wiley gold
    OA serve HTML, not a PDF link). Walk-found papers are mostly publisher records with no arXiv id, so without this
    they fall to abstract-only. Both empty for closed papers."""
    key = p["ids"].get("openalex") or (f"doi:{p['ids']['doi']}" if p["ids"].get("doi") else None)
    if not key:
        return [], []
    st, w = http_json(_oa(f"https://api.openalex.org/works/{key}?select=open_access,locations,best_oa_location"), rv.cache_dir)
    if st != 200 or not w:
        return [], []
    locs = [l for l in (w.get("locations") or []) + [w.get("best_oa_location") or {}] if l]
    pdf = [l for l in locs if l.get("pdf_url")]
    pdf.sort(key=lambda l: ((l.get("source") or {}).get("type") != "repository"))
    land = [l["landing_page_url"] for l in locs if l.get("is_oa") and l.get("landing_page_url")]
    if (w.get("open_access") or {}).get("is_oa") and p["ids"].get("doi"):
        land.append(f"https://doi.org/{p['ids']['doi']}")
    return list(dict.fromkeys(l["pdf_url"] for l in pdf)), list(dict.fromkeys(land))


def oa_pdf_urls(rv: Review, p: dict) -> list[str]:
    return oa_locations(rv, p)[0]


_NOT_A_PAPER = re.compile(r"just a moment|security verification|client challenge|enable javascript|captcha|access denied", re.I)
# a publisher page cut off after the abstract and first paragraphs (IEEE Xplore without a subscription, P381 in dashcam)
_PAYWALL = re.compile(r"sign in to continue reading|you do not have access to this pdf|purchase (this )?(article|pdf)|get full access"
                      r"|buy (this )?article|log ?in to (read|access) the full", re.I)
PAGE_MIN_PROSE = 12000   # dashcam: real jina-oa pages carry 33k-86k chars of prose, the IEEE paywall page 3k


def _prose_chars(text: str) -> int:
    """Characters in paragraph lines of a jina markdown page: long lines that are not links, list items, images or tables."""
    return sum(len(ln) for ln in text.splitlines()
               if len(ln.strip()) >= 150 and not ln.lstrip().startswith(("[", "*", "!", "|")) and ln.count("](") < 3)


def _looks_like_paper(text: str | None) -> bool:
    """A fetched page is the paper's text, not a bot check, a paywall or an abstract-only landing page."""
    if not text or len(text) < 5000 or _NOT_A_PAPER.search(text[:3000]) or _PAYWALL.search(text):
        return False
    low = text.lower()
    return "introduction" in low and ("references" in low or "conclusion" in low) and _prose_chars(text) >= PAGE_MIN_PROSE


def arxiv_by_title(rv: Review, title: str | None) -> dict | None:
    """The arXiv entry whose title matches `title` (free: an arXiv ti: word-AND search), or None."""
    words = [w for w in re.split(r"[^\w]+", title or "") if len(w) > 1 and w.lower() not in _TITLE_STOP]
    if not words:
        return None
    _, es = arxiv_query(rv, search_query=" AND ".join(f"ti:{w}" for w in words[:12]), max_results=5)
    return next((x for x in es if _title_match(x["title"], title)), None)


_ARXIV_VERSION = re.compile(r"arXiv:\d{4}\.\d{4,5}v(\d+)\s*\[[^\]]+\]\s*(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})")


def text_version_gap(rv: Review, pid: str) -> dict | None:
    """When the cached text is an arXiv version older than the paper's journal version: {"read": "arXiv v2, Jun 2024",
    "published": "npj Digital Medicine 2025"}, else None. Meerkat's npj version (2025) adds a physician evaluation that
    its arXiv v2 (2024) lacks; the review read v2 and said no evaluation used expert grades (llm-to-domain-slm)."""
    p = rv.tables["papers"].get(pid) or {}
    pv = ((p.get("published_venue") or {}).get("value") or {})
    _, meta = rv.read_fulltext(pid) if rv.fulltext_path(pid).exists() else (None, {})
    if meta.get("method") not in ("html", "jina", "pdf-local") or pv.get("kind") != "journal" or not pv.get("year"):
        return None
    m = _ARXIV_VERSION.search(rv.fulltext_path(pid).read_text(errors="replace")[:20000])
    if not m or int(pv["year"]) <= int(m.group(4)):
        return None
    return {"read": f"arXiv v{m.group(1)}, {m.group(3)} {m.group(4)}", "published": f"{pv.get('name')} {pv['year']}"}


def _oa_published_text(rv: Review, p: dict, skip_arxiv: bool = False) -> tuple:
    """(text, method, url) from the paper's open-access locations: PDFs (repository copies first), then landing pages
    read through jina. With skip_arxiv, an arXiv copy does not count (the journal version is wanted)."""
    pdfs, lands = oa_locations(rv, p)
    keep = lambda u: not (skip_arxiv and "arxiv" in u.lower())      # arxiv.org and doi.org/10.48550/arXiv.* alike
    for u in filter(keep, pdfs):
        local = pdf_text_local(u, rv.cache_dir)
        if local and len(local) > 5000:
            return local, "pdf-oa", u
    for u in filter(keep, lands):
        # gold-OA publishers that serve HTML (MDPI, IEEE, Wiley, Nature): read the landing page through jina
        st, body = http_get(f"https://r.jina.ai/{u}", rv.cache_dir)
        if st == 200 and _looks_like_paper(body):
            return body, "jina-oa", u
    return None, None, None


def cmd_fulltext(rv: Review, pid: str, force: bool = False, published: bool = False) -> dict:
    """Cache one paper's full text. `published` reads the open-access journal version first (text_version_gap: it is
    newer than the arXiv text) and keeps the cached text when there is none; step 5 must then run again for the paper."""
    p = rv.tables["papers"][pid]
    fp = rv.fulltext_path(pid)
    if published:
        text, method, url = _oa_published_text(rv, p, skip_arxiv=True)
        if text is None:
            return {"paper": pid, "method": None, "published": False,
                    "note": "no open-access copy of the published version found; the cached text is unchanged"}
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(text)
        rv.write_fulltext_meta(pid, url=url, method=method)
        return {"paper": pid, "method": method, "chars": len(text), "path": str(fp), "published": True,
                "next": f"run step 5 again for {pid}, then `merge-details --replace {pid}` (its quotes were checked against the "
                        "old text); rewrite its relevance and contribution notes (step 4); point its own KPI rows at their "
                        "benchmark and split labels again; rerun its family's slide and critique writers"}
    if fp.exists() and not force:
        _, meta = rv.read_fulltext(pid)
        return {"paper": pid, "method": meta.get("method"), "cached": True}
    ax = p["ids"].get("arxiv")
    if not ax:
        # walk-found publisher records (CVPR, IEEE, Springer) usually have an arXiv version OpenAlex does not link;
        # without its id the paper fell to abstract-only (TransGeo, L2LTR, Sample4Geo in the dashcam review)
        hit = arxiv_by_title(rv, p.get("title"))
        held = rv.find_paper({"arxiv": hit["arxiv_id"]}) if hit else None
        if hit and (held is None or held["id"] == pid):
            ax = p["ids"]["arxiv"] = hit["arxiv_id"]
    text, method, url = None, None, None
    if ax:
        url = f"https://arxiv.org/html/{ax}"
        st, body = http_get(url, rv.cache_dir)
        if st == 200 and "ltx_document" in body or (st == 200 and len(body) > 20000):
            text, method = html_to_text(body), "html"
        else:
            url = f"https://r.jina.ai/https://arxiv.org/pdf/{ax}"
            st, body = http_get(url, rv.cache_dir)
            if st == 200 and len(body) > 5000:
                text, method = body, "jina"
            else:
                url = f"https://arxiv.org/pdf/{ax}"
                local = pdf_text_local(url, rv.cache_dir)
                if local and len(local) > 5000:
                    text, method = local, "pdf-local"
    if text is None:
        text, method, url = _oa_published_text(rv, p)
    if text is None:
        text, method, url = f"Title: {p.get('title')}\n\nAbstract: {p.get('abstract') or ''}", "abstract-only", p.get("url")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(text)
    rv.write_fulltext_meta(pid, url=url, method=method)
    return {"paper": pid, "method": method, "chars": len(text), "path": str(fp)}


_pwc_links_cache = None
_pwc_eval_cache = None


class _FigParser(HTMLParser):
    """Top-level <figure class="ltx_figure"> elements (not ltx_table) with their images and main caption.
    Images are <img src> or <object class="ltx_graphics" data> (SVG). arXiv teasers often put the image in a
    sibling <div> right before an empty captioned <figure>; such a figure adopts the last loose image seen
    since the previous figure (of any kind, tables included) closed. A caption formula is kept once, as the TeX
    in <math alttext> (as html_to_text keeps it): its MathML text nodes, glued with U+200B, are skipped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base, self.figs, self.cur, self.depth, self.math = None, [], None, 0, 0
        self.loose: list[str] = []
        self.cap_depth: list[int] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "math":
            if not self.math and a.get("alttext"):
                self.handle_data(" " + a["alttext"] + " ")
            self.math += 1
        elif self.math:
            return
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag == "figure":
            if self.cur is None:
                if "ltx_figure" in cls and "ltx_table" not in cls:
                    self.cur, self.depth = {"images": [], "main": [], "sub": []}, 1
            else:
                self.depth += 1
        elif (tag == "img" and a.get("src")) or (tag == "object" and a.get("data") and "ltx_graphics" in cls):
            src = a.get("src") or a.get("data")
            if "/static/" not in src:
                (self.cur["images"] if self.cur is not None else self.loose).append(src)
        elif tag == "figcaption" and self.cur is not None:
            self.cap_depth.append(self.depth)

    def handle_endtag(self, tag):
        if self.math:
            self.math -= tag == "math"
        elif tag == "figcaption" and self.cap_depth:
            self.cap_depth.pop()
        elif tag == "figure":
            if self.cur is None:
                self.loose = []          # a table figure closed: its images are not loose teaser images
                return
            self.depth -= 1
            if self.depth == 0:
                if not self.cur["images"] and self.loose:
                    self.cur["images"] = self.loose[-1:]
                self.figs.append(self.cur)
                self.cur, self.loose = None, []

    def handle_data(self, data):
        if self.cur is not None and self.cap_depth and not self.math:
            self.cur["main" if self.cap_depth[-1] == 1 else "sub"].append(data)


def extract_figures(html_text: str, page_url: str) -> list[dict]:
    p = _FigParser()
    p.feed(html_text or "")
    base = urllib.parse.urljoin(page_url, p.base) if p.base else page_url
    out, seen = [], set()
    for i, f in enumerate(p.figs, 1):
        imgs = [urllib.parse.urljoin(base, s) for s in f["images"]]
        if not imgs:
            continue
        cap = re.sub(r"\s+", " ", " ".join(f["main"]) or " ".join(f["sub"])).strip()
        cap = re.sub(r"\s+([:.,;])", r"\1", cap)
        m = re.search(r"\b(?:Fig(?:ure)?\.?)\s*(S?\d+)", cap)
        label = f"Figure {m.group(1)}" if m else f"Unnumbered figure {i}"
        while label in seen:
            label += "'"
        seen.add(label)
        cap = cap if len(cap) <= 600 else cap[:601].rsplit(" ", 1)[0]     # cut between words: never inside a formula
        out.append({"label": label, "url": imgs[0], "images": imgs, "caption": cap})
    return out


def cmd_figures(rv: Review, pid: str) -> dict:
    p = rv.tables["papers"][pid]
    _, meta = rv.read_fulltext(pid)
    method, url = meta.get("method"), meta.get("url")
    if not meta:
        e = evidence(None, "inaccessible", checked=today(), rationale="no cached full text; run `fulltext` first")
    elif method != "html":
        e = evidence(None, "inaccessible", checked=today(), rationale=f"full text came from {method}, not arXiv HTML; figures not extracted")
    else:
        st, body = http_get(url, rv.cache_dir)
        figs = extract_figures(body, url) if st == 200 else []
        if st != 200:
            e = evidence(None, "inaccessible", checked=today(), rationale=f"arXiv HTML returned HTTP {st}")
        elif not figs:
            e = evidence(None, "not-found", checked=today(), rationale="no <figure class=ltx_figure> with an image in the arXiv HTML")
        else:
            e = evidence(figs, "verified", source=url, checked=today(), location="arXiv HTML <figure> elements")
    p["figures"] = e
    return {"paper": pid, "status": e["status"], "figures": len(e["value"] or [])}


def pwc_links_for(arxiv_id: str | None, title: str | None) -> list[dict]:
    global _pwc_links_cache
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return []
    files = sorted(PWC_CACHE.glob("links_*.parquet"))
    if not files:
        return []
    import pyarrow as pa
    import pyarrow.compute as pc
    if _pwc_links_cache is None:      # pyarrow only: the app's Python has no pandas (Depth Anything app run, 2026-10-12)
        _pwc_links_cache = pa.concat_tables([pq.read_table(f) for f in files])
    t = _pwc_links_cache
    hits = t.filter(pc.equal(t["paper_arxiv_id"], arxiv_id)) if arxiv_id else t.slice(0, 0)
    if hits.num_rows == 0 and title:
        hits = t.filter(pc.equal(pc.utf8_lower(t["paper_title"]), title.lower()))
    return hits.to_pylist()


def pwc_index_path() -> Path:
    return PWC_CACHE / "eval_index.json"


def build_pwc_index(progress=print) -> dict:
    """One-time flatten of pwc-archive evaluation-tables into {arxiv:<id> | title:<norm>: [rows]}.
    Uses pyarrow columnar ops: the row struct has ~3.5k mostly-null metric fields, so a Python
    to_pylist takes >30 min while list_flatten + struct_field takes seconds."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    files = sorted(PWC_CACHE.glob("eval_*.parquet"))
    if not files:
        raise SystemExit(f"no eval_*.parquet in {PWC_CACHE}; see sources.md for download URLs")
    index: dict[str, list] = {}
    n = 0

    def has_field(typ, name):
        return any(f.name == name for f in typ)

    def emit(task_names: list, ds_arr, parent_ds: list | None = None):
        """ds_arr: list<struct dataset> array aligned with task_names (one list per task)."""
        nonlocal n
        if ds_arr.null_count == len(ds_arr):
            return
        flat = pc.list_flatten(ds_arr)
        if len(flat) == 0:
            return
        par = pc.list_parent_indices(ds_arr).to_pylist()
        ds_task = [task_names[i] for i in par]
        ds_name = pc.struct_field(flat, "dataset").to_pylist()
        if parent_ds is not None:
            ds_name = [f"{parent_ds[i]} / {d}" if d else parent_ds[i] for i, d in zip(par, ds_name)]
        links = pc.struct_field(flat, "dataset_links").to_pylist()
        lb = [next((l.get("url") for l in (ls or []) if "sota" in (l.get("url") or "")), None) for ls in links]
        sota = pc.struct_field(flat, "sota")
        rows = pc.struct_field(sota, "rows")
        if rows.null_count < len(rows):
            frows = pc.list_flatten(rows)
            if len(frows):
                rpar = pc.list_parent_indices(rows).to_pylist()
                nrows = pc.list_value_length(rows).fill_null(0).to_pylist()
                cols = {f: pc.struct_field(frows, f).to_pylist() for f in ("model_name", "paper_title", "paper_url", "paper_date", "uses_additional_data")}
                metrics = pc.struct_field(frows, "metrics")
                mt = pa.Table.from_arrays([metrics], names=["m"]).flatten()
                keep = [c for c in mt.column_names if mt[c].null_count < mt.num_rows]
                per_row: list[dict] = [dict() for _ in range(len(frows))]
                for c in keep:
                    col = mt[c]
                    name = c.split(".", 1)[1].strip() if "." in c else c.strip()
                    if not name:
                        continue
                    valid = pc.indices_nonzero(pc.is_valid(col)).to_pylist()
                    vals = col.take(pa.array(valid)).to_pylist()
                    for i, v in zip(valid, vals):
                        if v not in (None, ""):
                            per_row[i][name] = v
                for i in range(len(frows)):
                    if not per_row[i]:
                        continue
                    d = rpar[i]
                    rec = {"task": ds_task[d], "dataset": ds_name[d], "model": cols["model_name"][i], "paper_url": cols["paper_url"][i],
                           "paper_title": cols["paper_title"][i], "paper_date": cols["paper_date"][i], "metrics": per_row[i],
                           "uses_additional_data": cols["uses_additional_data"][i], "n_rows": nrows[d], "leaderboard_url": lb[d]}
                    keys = set()
                    a = norm_arxiv(rec["paper_url"] or "")
                    if a:
                        keys.add("arxiv:" + a)
                    t = norm_title(rec["paper_title"])
                    if t:
                        keys.add("title:" + t)
                    for k in keys:
                        index.setdefault(k, []).append(rec)
                    n += 1
        if has_field(flat.type, "subdatasets"):
            sub = pc.struct_field(flat, "subdatasets")
            if sub.null_count < len(sub) and pa.types.is_list(sub.type) and pa.types.is_struct(sub.type.value_type) and has_field(sub.type.value_type, "sota"):
                emit(ds_task, sub, parent_ds=ds_name)

    def walk(task_names: list, tasks_struct_arr):
        if has_field(tasks_struct_arr.type, "datasets"):
            emit(task_names, pc.struct_field(tasks_struct_arr, "datasets"))
        if has_field(tasks_struct_arr.type, "subtasks"):
            subs = pc.struct_field(tasks_struct_arr, "subtasks")
            if subs.null_count < len(subs) and pa.types.is_list(subs.type) and pa.types.is_struct(subs.type.value_type):
                flat = pc.list_flatten(subs)
                if len(flat):
                    par = pc.list_parent_indices(subs).to_pylist()
                    names = pc.struct_field(flat, "task").to_pylist() if has_field(flat.type, "task") else [None] * len(flat)
                    names = [nm or task_names[par[i]] for i, nm in enumerate(names)]
                    walk(names, flat)

    for f in files:
        tbl = pq.read_table(f)
        arr = tbl.to_struct_array().combine_chunks() if hasattr(tbl, "to_struct_array") else pa.StructArray.from_arrays(tbl.columns, names=tbl.column_names)
        walk(tbl["task"].to_pylist(), arr)
        progress(f"{f.name}: {n} rows indexed")
    PWC_CACHE.mkdir(parents=True, exist_ok=True)
    pwc_index_path().write_text(json.dumps(index))
    return {"rows": n, "keys": len(index), "path": str(pwc_index_path())}


_pwc_index = None


def pwc_results_for(arxiv_id: str | None, title: str | None) -> list[dict]:
    """Leaderboard rows for a paper from the pwc-archive index (built by `pwc-index`). Empty if no index."""
    global _pwc_index
    ip = pwc_index_path()
    if not ip.exists():
        return []
    if _pwc_index is None:
        _pwc_index = json.loads(ip.read_text())
    out = []
    if arxiv_id:
        out += _pwc_index.get("arxiv:" + arxiv_id, [])
    if not out and title:
        out += _pwc_index.get("title:" + norm_title(title), [])
    return out


def _official_link_rank(url: str, cand_by_url: dict, p: dict) -> tuple:
    """Sort key for pwc-archive repos flagged official: the repo the paper text names (by its text score), then an
    owner that matches an author, then a repo name holding the paper's short name ('AWQ' -> llm-awq)."""
    u = url.lower().rstrip("/")
    c = cand_by_url.get(u) or cand_by_url.get(u + "/")
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", u)
    owner, repo = (norm_name(m.group(1)), norm_name(m.group(2))) if m else ("", "")
    title = p.get("title") or ""
    short = norm_name(title.split(":")[0]) if ":" in title else ""
    return (-(c["score"] if c else -99), not any(t in owner for t in _author_tokens(p.get("authors")) if len(t) >= 5),
            not (short and short in repo))


def cmd_enrich(rv: Review, pids: list[str]) -> dict:
    report = {}
    if not pwc_index_path().exists():
        report["_warning"] = f"pwc-archive index missing ({pwc_index_path()}); historical leaderboard rows skipped. Run `pwc-index` once (≈10 s)."
    for pid in pids:
        p = rv.tables["papers"][pid]
        ax = p["ids"].get("arxiv")
        notes = []
        if not p["ids"].get("openalex"):
            openalex_resolve(rv, p)
        # --- code ---
        # order: HF paper page -> pwc-archive official -> arXiv comment -> a repo linked from the project page the arXiv
        # comment or the paper's first page names -> the paper's own repo printed in its text
        # (scored by context, see score_repo_candidates) -> a pwc-archive non-official link that names the paper.
        code_url, code_kind, code_src, code_status, code_notes = None, None, None, None, None
        text = rv.read_fulltext(pid)[0] or ""
        cands = score_repo_candidates(text, p.get("title"), p.get("authors"))
        cand_by_url = {c["url"].lower(): c for c in cands}
        hfp = None
        if ax:
            st, hfp = http_json(f"https://huggingface.co/api/papers/{ax}", rv.cache_dir)
            if hfp and hfp.get("githubRepo"):
                code_url, code_kind, code_src, code_status = hfp["githubRepo"], "unknown", f"https://huggingface.co/api/papers/{ax}", "verified"
                code_notes = f"githubRepo linked on HF paper page ({hfp.get('githubStars')} stars)"
        links = pwc_links_for(ax, p.get("title"))
        official = sorted((l for l in links if l.get("is_official")), key=lambda l: _official_link_rank(l["repo_url"], cand_by_url, p))
        if official:
            # pwc-archive can flag several repos (AWQ: lmdeploy, TensorRT-LLM, llm-awq, vLLM); the paper's own comes first
            code_url, code_kind, code_status = official[0]["repo_url"], "official", "verified"
            code_src = "https://huggingface.co/datasets/pwc-archive/links-between-paper-and-code"
            code_notes = f"is_official=true in pwc-archive snapshot {PWC_SNAPSHOT}" + (f"; {len(links)} repos total" if len(links) > 1 else "")
        if not code_url and p.get("comment"):
            m = re.search(r"https?://github\.com/[^\s,;)]+", p["comment"])
            if m:
                code_url, code_kind, code_status, code_src = m.group(0), "official", "author-reported", p.get("url")
                code_notes = "GitHub URL given in the arXiv comment field"
        code_quote, skipped = None, []
        if not code_url:
            pcs = project_page_repos(rv, p, text)
            ok = [c for c in pcs if c["score"] >= CODE_REPO_MIN_SCORE]
            if ok:
                c = ok[0]
                code_url, code_kind, code_status, code_src = c["url"], "official", "author-reported", c["page"]
                code_notes = f"linked from the paper's project page {c['page']} (own-code score {c['score']}: {'; '.join(c['why'])})"
            skipped += [f"{c['url']} (project page link, score {c['score']})" for c in pcs if c["score"] < CODE_REPO_MIN_SCORE][:4]
        if not code_url:
            for c in [c for c in cands if c["score"] >= CODE_REPO_MIN_SCORE][:3]:
                st, _ = http_get(c["url"], rv.cache_dir, ttl=TTL_SEARCH)
                if st == 200:
                    code_quote = c["quote"]
                    code_url, code_kind, code_status, code_src = c["url"], "official", "verified", c["url"]
                    code_notes = (f"repository URL printed in the paper text (own-code score {c['score']}: {'; '.join(c['why'])}); "
                                  f"fetched HTTP 200 (contents not inspected)")
                    break
                skipped.append(f"{c['url']} (HTTP {st})")
        if not code_url:
            third = [l for l in links if not l.get("is_official")]
            ok = [l for l in third if plausible_third_party(l, p.get("title"))]
            if ok:
                code_url, code_kind, code_status = ok[0]["repo_url"], "third-party", "verified"
                code_src = "https://huggingface.co/datasets/pwc-archive/links-between-paper-and-code"
                code_notes = f"third-party implementation per pwc-archive snapshot {PWC_SNAPSHOT}"
            skipped += [f"{l['repo_url']} (pwc-archive non-official, not mentioned in the paper)" for l in third if l not in ok]
        if code_url:
            if code_kind == "unknown":
                m = re.search(r"github\.com/([^/]+)/", code_url)
                owner = norm_name(m.group(1)) if m else ""
                in_text = cand_by_url.get(code_url.lower().rstrip("/"))
                if in_text and in_text["score"] >= CODE_REPO_MIN_SCORE:
                    code_kind = "official"
                    code_notes += "; the paper text names this repo as its own"
                elif owner and any(t in owner for t in _author_tokens(p.get("authors"))):
                    code_kind = "official"
                elif not in_text and not plausible_third_party({"repo_url": code_url}, p.get("title")):
                    code_notes += "; not corroborated by the paper text, the author names or the title (check it)"
            st, _ = http_get(code_url, rv.cache_dir, ttl=TTL_SEARCH)
            if st >= 400:
                code_notes = (code_notes or "") + f"; URL returned HTTP {st} on {today()}"
            # a recorded 'no' (P1753: the repo its arXiv comment names is gone) beats the same dead link found again
            if not (st >= 400 and ((p.get("code") or {}).get("value") or {}).get("availability") == "no"):
                p["code"] = evidence({"availability": "yes", "url": code_url, "kind": code_kind, "notes": code_notes}, code_status, source=code_src, checked=today(),
                                     location="paper text" if code_quote else None, quote=code_quote)
        elif ((p.get("code") or {}).get("value") or {}).get("availability") == "yes":
            pass   # keep a repo found earlier (by hand or by an older rule: a URL split across a PDF line, a Zenodo DOI)
        else:
            weak = [f"{c['url']} (score {c['score']})" for c in cands if c["score"] < CODE_REPO_MIN_SCORE][:4]
            p["code"] = evidence(None, "not-found", checked=today(),
                                 rationale="no repo on HF paper page, pwc-archive official links, arXiv comment or its project page, and no repo printed in the "
                                           "text is clearly the paper's own; availability unknown (not a verified no)"
                                           + (f"; printed repos judged third-party: {', '.join(weak)}" if weak else "")
                                           + (f"; skipped: {', '.join(skipped)}" if skipped else ""))
        # --- checkpoint ---
        if ax:
            st, models = http_json(f"https://huggingface.co/api/models?filter=arxiv:{ax}&limit=50&sort=downloads&direction=-1", rv.cache_dir, ttl=TTL_SEARCH)
            cls = classify_checkpoints(models or [], p.get("authors") or [], code_url if code_url and "github.com" in code_url else (hfp or {}).get("githubRepo"))
            src = f"https://huggingface.co/api/models?filter=arxiv:{ax}"
            if cls["availability"] == "yes":
                p["checkpoint"] = evidence({"availability": "yes", "kind": cls["kind"],
                                            "url": f"https://huggingface.co/{(cls['official'] or cls['community'])[0]}",
                                            "official_models": cls["official"][:10], "community_models": len(cls["community"]),
                                            "notes": "HF filter=arxiv returns models citing the paper; 'official' = namespace matches an author or the code repo"},
                                           "verified", source=src, checked=today())
            else:
                p["checkpoint"] = evidence(None, "not-found", checked=today(), rationale="no HF model cites this arXiv id; availability unknown (not a verified no)")
            # no official HF model: the paper may print its own HF weights (Meerkat: the model card cites other papers'
            # arXiv ids, so filter=arxiv lists only strangers' conversions). A link must resolve on the HF API: a 'will
            # release' promise that was never kept is not weights.
            text_weights = []
            if cls["kind"] != "official":
                for c in [c for c in score_weights_candidates(text, p.get("title"), p.get("authors"), code_url) if c["score"] >= WEIGHTS_MIN_SCORE][:5]:
                    if c.get("collection"):
                        _, owner, slug = c["id"].split("/", 2)
                        text_weights += [dict(c, id=mid, url=f"https://huggingface.co/{mid}", collection=False,
                                              why=c["why"] + [f"in the authors' collection {c['id']}"])
                                         for mid in hf_collection_models(rv, owner, slug) if mid not in [x["id"] for x in text_weights]]
                    elif hf_model_facts(rv, c["id"]) or http_json(f"https://huggingface.co/api/models/{c['id']}", rv.cache_dir, ttl=TTL_SEARCH)[0] == 200:
                        text_weights.append(c)
            if text_weights:
                order = {m.lower(): i for i, m in enumerate(cls["community"])}   # HF's list is sorted by downloads
                text_weights.sort(key=lambda c: order.get(c["id"].lower(), 10 ** 6))
                c = text_weights[0]
                p["checkpoint"] = evidence({"availability": "yes", "kind": "official", "url": c["url"],
                                            "official_models": [x["id"] for x in text_weights],
                                            "community_models": len(cls["community"]),
                                            "notes": f"Hugging Face model printed in the paper text as its own (score {c['score']}: {'; '.join(c['why'])}); "
                                                     "HF API record found"
                                                     + (f"; HF also lists {len(cls['community'])} community model(s) citing the paper" if cls["community"] else "")},
                                           "verified", source=c["url"], checked=today(), location="paper text", quote=c["quote"][:300])
            # no official HF model: read the official repo's README for released weights. Also when HF lists only
            # community conversions (MoGe-2: 4 tagged conversions, official weights under Ruicheng/ are not tagged)
            gm = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", code_url or "")
            if gm and code_kind == "official" and cls["kind"] != "official" and not text_weights:
                for br in ("main", "master"):
                    rurl = f"https://raw.githubusercontent.com/{gm.group(1)}/{gm.group(2)}/{br}/README.md"
                    st, md = http_get(rurl, rv.cache_dir, ttl=TTL_SEARCH)
                    if st != 200:
                        continue
                    cited = {m.lower() for m in cls["community"]}
                    line = readme_checkpoint_line(md, p.get("title"), p.get("authors"), code_url, cited=cited)
                    if line:
                        wus = [u for u in re.findall(r"https?://[^\s\"'<>)`\]]+", line) if _CKPT_HOST.search(u)
                               and (_own_weights_link(u, p.get("title"), p.get("authors"), code_url)
                                    or (re.search(r"huggingface\.co/([^/?#\s]+/[^/?#\s`)\]]+)", u) or [None, ""])[1].lower() in cited)]
                        wurl = wus[0] if wus else code_url
                        p["checkpoint"] = evidence({"availability": "yes", "kind": "official", "url": wurl,
                                                    "community_models": len(cls["community"]),
                                                    "notes": "weights linked from the official repository README (external host; link not downloaded)"
                                                             + (f"; HF also lists {len(cls['community'])} community model(s) citing the paper" if cls["community"] else "")},
                                                   "verified", source=rurl, checked=today(), location="README", quote=line)
                    elif p["checkpoint"].get("status") == "not-found":
                        p["checkpoint"]["rationale"] += "; official README read, no released-weights link found"
                    break
        else:
            p["checkpoint"] = evidence(None, "inaccessible", checked=today(), rationale="no arXiv id; HF model lookup not possible")
        # the authors' HF weights: licence, size and loader, which decide whether a team can evaluate them as they are
        ck = (p.get("checkpoint") or {}).get("value") or {}
        hm = re.match(r"https://huggingface\.co/([^/?#\s]+/[^/?#\s]+)", ck.get("url") or "")
        if ck.get("kind") == "official" and hm:
            ck.update(hf_model_facts(rv, hm.group(1)))
            # every release's size: CURE-Med's first model is 1.5B, its comparison uses the 7B one, and it ships up to 32B
            sizes = [x for x in (hf_model_facts(rv, m).get("params") for m in (ck.get("official_models") or [])[:10]) if x]
            if len(set(sizes)) > 1:
                ck["params_range"] = [min(sizes), max(sizes)]
        # --- labs ---
        if not is_evidence(p.get("labs")):
            p["labs"] = evidence(None, "not-found", checked=today(), rationale="no institutions in OpenAlex authorships")
        # --- historical leaderboard rows (pwc-archive index; empty until `pwc-index` has been run) ---
        n_res = 0
        for r in pwc_results_for(ax, p.get("title")):
            if is_placeholder_name(r.get("dataset")):
                continue      # a junk board in the archive (P3068: '^(#$!@#$)(()))******', model 'Abc')
            d = rv._find_by_name("datasets", r["dataset"]) or rv.add_dataset(r["dataset"], status="candidate")
            for mname, mval in r["metrics"].items():
                m = rv._find_by_name("metrics", mname) or rv.add_metric(mname, status="candidate")
                try:
                    val = float(str(mval).replace("%", "").replace(",", ""))
                except ValueError:
                    val = mval
                dup = next((x for x in rv.tables["results"].values() if x["paper_id"] == pid and x["dataset_id"] == d["id"] and x["metric_id"] == m["id"]
                            and x.get("provenance") == "leaderboard-verified" and x.get("method_variant") == r.get("model")), None)
                if dup:
                    continue
                rv.add_result(paper_id=pid, method_variant=r.get("model"), dataset_id=d["id"], metric_id=m["id"], value=val,
                              provenance="leaderboard-verified", snapshot_date=PWC_SNAPSHOT, status="verified",
                              source=r.get("leaderboard_url") or "https://huggingface.co/datasets/pwc-archive/evaluation-tables",
                              location=f"pwc-archive evaluation-tables: task {r['task']!r} / dataset {r['dataset']!r} / model {r.get('model')!r}",
                              training_data=("uses additional data" if r.get("uses_additional_data") else None),
                              caveats=f"historical Papers With Code leaderboard snapshot ({PWC_SNAPSHOT}); site offline; {r['n_rows']} rows on that board; rank not taken from snapshot ordering")
                n_res += 1
        report[pid] = {"code": p["code"]["status"], "checkpoint": p["checkpoint"]["status"], "labs": p["labs"]["status"], "pwc_results": n_res}
    rv.derive()
    return report


def dataset_info(rv: Review, name: str) -> dict:
    """Facts a step-3 subagent can cite: pwc-archive datasets row (homepage, license, modalities, tasks,
    variants, intro date) and Hugging Face dataset-card hits. Each hit carries its own source URL."""
    out = {"name": name, "pwc_archive": [], "huggingface": []}
    try:
        import pyarrow.parquet as pq
        files = sorted(PWC_CACHE.glob("ds_*.parquet"))
        k = norm_name(name)
        for f in files:
            t = pq.read_table(f, columns=["url", "name", "full_name", "homepage", "description", "introduced_date", "license_name", "license_url",
                                          "modalities", "tasks", "variants", "num_papers", "paper"])
            names = [norm_name(x) for x in t["name"].to_pylist()]
            fulls = [norm_name(x) for x in t["full_name"].to_pylist()]
            for i, (a, b) in enumerate(zip(names, fulls)):
                if k and (a == k or b == k):
                    row = {c: t[c][i].as_py() for c in t.column_names}
                    row["description"] = (row.get("description") or "")[:600]
                    row["source"] = "https://huggingface.co/datasets/pwc-archive/datasets"
                    row["snapshot_date"] = PWC_SNAPSHOT
                    out["pwc_archive"].append(row)
    except ImportError:
        out["pwc_archive_error"] = "pyarrow not installed"
    url = f"https://huggingface.co/api/datasets?search={urllib.parse.quote(name)}&limit=5"
    st, js = http_json(url, rv.cache_dir, ttl=TTL_SEARCH)
    for d in js or []:
        out["huggingface"].append({"id": d.get("id"), "url": f"https://huggingface.co/datasets/{d.get('id')}", "downloads": d.get("downloads"),
                                   "tags": (d.get("tags") or [])[:12], "source": url})
    return out


# --------------------------------------------------------------------------
# generic editing (for the agent / subagents)
# --------------------------------------------------------------------------

def cmd_import(rv: Review, table: str, records: list[dict], report: list | None = None) -> list[str]:
    """Upsert records into a table. Papers go through upsert_paper; others match by id or exact name/alias.
    `report` (if given) gets one 'created X' / 'updated X ...' line per named record."""
    out = []
    report = report if report is not None else []
    for rec in records:
        if table == "papers":
            p = rv.tables["papers"].get(rec.get("id") or "")
            if p is None and rec.get("id"):
                raise SystemExit(f"{rec['id']} not in papers; omit id to create a new paper")
            if p is None:
                p = rv.upsert_paper(rec)
            for k, v in rec.items():
                if k in ("id", "ids") or v is None:
                    continue
                p[k] = v
            out.append(p["id"])
            continue
        tbl = rv.tables[table]
        if table == "discovery_links":
            rv.add_link(rec["run_id"], rec["paper_id"], rec["discovery_type"], rec.get("origin"), rec.get("decision", "candidate"), rec.get("reason"))
            out.append(f"{rec['run_id']}/{rec['paper_id']}")
            continue
        e = tbl.get(rec.get("id") or "")
        how = "by id" if e is not None else None
        if e is None and rec.get("name"):
            e = rv._find_by_name(table, rec["name"], exact=True) if table in ("tasks", "datasets", "benchmarks", "metrics") else None
            how = "matched by name" if e is not None else None
        if e is None and table == "search_terms" and rec.get("term"):
            e = rv.add_term(rec["term"], rec.get("category", "concept"), rec.get("origin", "manual"), rec.get("rationale", ""), rec.get("aliases"))
        if e is None:
            adder = {"tasks": rv.add_task, "datasets": rv.add_dataset, "benchmarks": rv.add_benchmark, "metrics": rv.add_metric}.get(table)
            if adder:
                e, how = adder(rec["name"], exact=True), "created"
            elif table == "results":
                e = rv.add_result()
            elif table == "search_runs":
                e = rv.add_run(rec.get("kind", "keyword"), rec.get("query", ""), rec.get("term_ids", []), rec.get("source", "manual"), rec.get("query_url"))
            else:
                raise SystemExit(f"cannot create records in {table}")
        for k, v in rec.items():
            if k == "id":
                continue
            e[k] = v
        out.append(e["id"])
        if table in ("tasks", "datasets", "benchmarks", "metrics"):
            report.append(f"created {e['id']}" if how == "created" else f"updated {e['id']} ({how or 'by id'})")
    rv.derive()
    return out


# --------------------------------------------------------------------------
# coverage + why-missing (what the review may be missing, and why a paper is not in it)
# --------------------------------------------------------------------------

COVERAGE_MIN_LINKS = 2


def _pool_index(rv: Review) -> dict:
    """Lookup keys (s2:, arxiv:, doi:, title:) -> paper id for every paper in the review, any status."""
    idx = {}
    for p in rv.tables["papers"].values():
        ids = p.get("ids") or {}
        for k in ("s2", "arxiv", "doi"):
            if ids.get(k):
                idx[f"{k}:{str(ids[k]).lower()}"] = p["id"]
        if p.get("title"):
            idx[f"title:{norm_title(p['title'])}"] = p["id"]
    return idx


def _pool_hit(rv: Review, idx: dict, w: dict) -> dict | None:
    ext = w.get("externalIds") or {}
    for key in (f"s2:{w.get('paperId')}", f"arxiv:{str(ext.get('ArXiv') or '').lower()}", f"doi:{str(ext.get('DOI') or '').lower()}",
                f"title:{norm_title(w.get('title'))}"):
        pid = idx.get(key)
        if pid and not key.endswith(":"):
            return rv.tables["papers"][pid]
    return None


def _cov_entry(w: dict, pool: dict | None) -> dict:
    ext = w.get("externalIds") or {}
    return {"title": w.get("title"), "year": w.get("year"), "citations": w.get("citationCount"), "s2": w.get("paperId"),
            "arxiv": ext.get("ArXiv"), "doi": ext.get("DOI"),
            "pool": {"paper_id": pool["id"], "status": pool.get("status"), "reason": pool.get("status_reason"),
                     "manual": bool(pool.get("manual"))} if pool else None}


def _coverage_gaps(rv: Review) -> dict:
    """Run-level causes of misses: reference walks that found nothing, and terms that were never searched. Forward walks
    cut at their top-N are not listed: nearly all are (top 50 by citations, by design), and the neighbour list reads up
    to 3000 citers per included paper, so a low-cited citer the walk dropped still shows up there."""
    runs = rv.tables["search_runs"].values()
    ok_ref = {s.get("seed_paper_id") for s in runs if s.get("kind") == "reference" and (s.get("retrieved") or 0) > 0}
    return {
        "zero_ref_walks": [{"run_id": s["id"], "paper_id": s.get("seed_paper_id"), "limitations": s.get("limitations")}
                           for s in runs if s.get("kind") == "reference" and not s.get("retrieved") and s.get("seed_paper_id") not in ok_ref],
        "skipped_terms": [{"id": t["id"], "term": t.get("term"), "status": t.get("status"), "reason": t.get("status_reason")}
                          for t in rv.tables["search_terms"].values() if t.get("status") in ("redundant", "deferred", "out-of-scope")],
    }


def coverage_review(rv: Review, limit: int = 100, survey_limit: int = 25, now_year: int | None = None) -> dict:
    """Alert-only coverage check, written to data/coverage.json. `neighbours`: works that cite or are cited by at least
    COVERAGE_MIN_LINKS included papers (Semantic Scholar edges), ranked by links then citations; `survey_refs`: the
    in-window bibliography of each walked survey, by citations. Neither list touches `papers`: screening stays manual.
    `gaps` lists the run-level causes of misses (empty reference walks, skipped terms)."""
    now_year = now_year or _dt.date.today().year
    caps = rv.meta["caps"]
    min_year = now_year - int(caps.get("years", 3))

    def in_window(w):   # the year window only: a pre-window classic (NYUv2, ResNet) is a known foundation, not a miss
        return (w.get("year") or 0) >= min_year

    papers = rv.tables["papers"]
    idx = _pool_index(rv)
    included = [p for p in papers.values() if p.get("status") == "included" and not p.get("is_survey")]
    agg, failed = {}, []
    for p in included:
        key = s2_paper_key(p)
        if not key:
            failed.append({"paper_id": p["id"], "direction": "both", "status": "no id"})
            continue
        for direction, label in (("backward", "cited-by"), ("forward", "cites")):
            works, st, _ = _s2_edges(rv, key, direction)
            if st != 200:
                failed.append({"paper_id": p["id"], "direction": direction, "status": st})
            for w in works:
                if not in_window(w):
                    continue
                hit = _pool_hit(rv, idx, w)
                if hit is not None and hit.get("status") == "included":
                    continue
                a = agg.setdefault(w["paperId"], {"w": w, "pool": hit, "via": []})
                if not any(v["paper_id"] == p["id"] and v["direction"] == label for v in a["via"]):
                    a["via"].append({"paper_id": p["id"], "direction": label})
    neigh = []
    for a in agg.values():
        links = len({v["paper_id"] for v in a["via"]})
        if links >= COVERAGE_MIN_LINKS:
            neigh.append(dict(_cov_entry(a["w"], a["pool"]), links=links, via=a["via"]))
    neigh.sort(key=lambda n: (-n["links"], -(n["citations"] or 0)))

    survey_refs = []
    walked = {s.get("seed_paper_id") for s in rv.tables["search_runs"].values() if s.get("kind") == "reference"}
    for p in sorted(papers.values(), key=lambda p: p["id"]):
        if not (p.get("is_survey") or p.get("taxonomy_source")) or p["id"] not in walked:
            continue
        key = s2_paper_key(p)
        works, st, _ = _s2_edges(rv, key, "backward") if key else ([], 0, None)
        if st != 200:
            failed.append({"paper_id": p["id"], "direction": "backward", "status": st if key else "no id"})
        refs = []
        for w in sorted(works, key=lambda w: -(w.get("citationCount") or 0)):
            hit = _pool_hit(rv, idx, w)
            if in_window(w) and not (hit is not None and hit.get("status") == "included"):
                refs.append(_cov_entry(w, hit))
        survey_refs.append({"survey_id": p["id"], "title": p.get("title"), "total": len(works), "refs": refs[:survey_limit]})

    gaps = _coverage_gaps(rv)
    cov = {"generated_at": now_iso(), "included_count": len(included), "window_from": min_year,
           "min_links": COVERAGE_MIN_LINKS, "neighbours": neigh[:limit], "neighbours_total": len(neigh),
           "survey_refs": survey_refs, "gaps": gaps, "failed": failed}
    (rv.path / "data").mkdir(parents=True, exist_ok=True)
    (rv.path / "data" / "coverage.json").write_text(json.dumps(cov, indent=1, ensure_ascii=False))
    return cov


def coverage_apply(rv: Review, cov: dict, rerun_walks: bool = True) -> dict:
    """Act on a coverage report the way `snowball` does: rerun reference walks that found nothing (they now fall back
    to S2, then the cached bibliography), and add every listed neighbour and survey reference to the pool as an
    unscreened `candidate` with one discovery link per included paper it is linked to. Nothing is included: `select`
    and `screen` decide. Skipped terms are left for a decision (searching them costs requests)."""
    rep = {"reran": [], "rerun_failed": [], "added": [], "linked": 0}
    if rerun_walks and not CACHE_ONLY:
        for z in cov["gaps"]["zero_ref_walks"]:
            try:
                r = cmd_graph(rv, z["paper_id"], "backward", 50)
                rep["reran"].append({"paper_id": z["paper_id"], "run": r.get("run"), "retrieved": r.get("retrieved")})
            except SystemExit as e:
                rep["rerun_failed"].append({"paper_id": z["paper_id"], "error": str(e)})
    n_lists = len(cov["neighbours"]) + sum(len(sv["refs"]) for sv in cov["survey_refs"])
    if not n_lists:
        return rep
    fields = dict(query=f"coverage: S2 neighbours linked to >= {cov['min_links']} included papers, "
                        f"and in-window references of walked surveys",
                  scope=f"top {len(cov['neighbours'])} neighbours, top {max([len(sv['refs']) for sv in cov['survey_refs']] or [0])} per survey",
                  retrieved=n_lists, filters={"coverage": cov["generated_at"]},
                  limitations=f"{len(cov['failed'])} S2 edge lists failed" if cov["failed"] else None)
    # one coverage run per review, reused on every call: add_link dedupes per run, so a rerun must not add a second
    # set of links (select counts every link, and a neighbour's 24 links would become 48)
    run = next((r for r in rv.tables["search_runs"].values() if r.get("kind") == "coverage"), None)
    if run is None:
        run = rv.add_run(kind="coverage", term_ids=[], source="semanticscholar", query_url=None, **fields)
    else:
        run.update(fields, executed_on=today())
    before = set(rv.tables["papers"])

    def upsert(e):
        if e.get("pool"):
            return rv.tables["papers"][e["pool"]["paper_id"]]
        w = {"paperId": e["s2"], "title": e["title"], "year": e["year"], "citationCount": e["citations"],
             "externalIds": {"ArXiv": e.get("arxiv"), "DOI": e.get("doi")}}
        p = rv.upsert_paper(s2_work_to_paper(w, f"https://api.semanticscholar.org/graph/v1/paper/{e['s2']}"), source_run_id=run["id"])
        e["pool"] = {"paper_id": p["id"], "status": p.get("status"), "reason": p.get("status_reason"), "manual": False}
        return p
    for n in cov["neighbours"]:
        p = upsert(n)
        for v in n["via"]:
            rv.add_link(run["id"], p["id"], "reference" if v["direction"] == "cited-by" else "citing", v["paper_id"],
                        p.get("status") or "candidate", f"coverage: {v['direction']} {v['paper_id']} ({n['links']} included papers linked)")
            rep["linked"] += 1
    for sv in cov["survey_refs"]:
        for e in sv["refs"]:
            p = upsert(e)
            rv.add_link(run["id"], p["id"], "survey", sv["survey_id"], p.get("status") or "candidate",
                        f"coverage: cited by survey {sv['survey_id']}")
            rep["linked"] += 1
    rep["added"] = sorted(set(rv.tables["papers"]) - before)
    rep["run"] = run["id"]
    rv.derive()
    (rv.path / "data" / "coverage.json").write_text(json.dumps(cov, indent=1, ensure_ascii=False))
    return rep


def load_coverage(rv: Review) -> dict | None:
    f = rv.path / "data" / "coverage.json"
    return json.loads(f.read_text()) if f.exists() else None


_ARXIV_Q = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?$", re.I)


def why_missing(rv: Review, query: str) -> dict:
    """Is a paper in the review, and if not, why not? Local only: papers, discovery links, data/coverage.json, runs."""
    q = query.strip()
    papers = rv.tables["papers"]
    m = _ARXIV_Q.match(q)
    hit = papers.get(q.upper()) if re.match(r"^[Pp]\d{3,}$", q) else None
    if hit is None and m:
        hit = next((p for p in papers.values() if ((p.get("ids") or {}).get("arxiv") or "").split("v")[0] == m.group(1)), None)
    nt = norm_title(q)
    if hit is None and nt:
        hit = next((p for p in papers.values() if norm_title(p.get("title")) == nt), None)
    out = {"query": q, "verdict": None, "paper": None, "paths": [], "notes": []}
    cov = load_coverage(rv)
    if hit is not None:
        out["verdict"] = "in-review" if hit.get("status") == "included" else "in-pool"
        out["paper"] = {"id": hit["id"], "title": hit.get("title"), "year": hit.get("year"), "status": hit.get("status"),
                        "reason": hit.get("status_reason"), "manual": bool(hit.get("manual"))}
        for l in rv.tables["discovery_links"]:
            if l["paper_id"] == hit["id"]:
                r = rv.tables["search_runs"].get(l["run_id"], {})
                out["paths"].append({"run_id": l["run_id"], "kind": r.get("kind"), "source": r.get("source"), "query": r.get("query"),
                                     "seed_paper_id": r.get("seed_paper_id"), "discovery_type": l.get("discovery_type"), "reason": l.get("reason")})
        if hit.get("status") != "included":
            out["notes"].append(f"{hit['id']} is {hit.get('status')}: {hit.get('status_reason') or 'no reason recorded'}")
            if hit.get("status") == "candidate":
                out["notes"].append("never screened: run `select`, or `screen` it in if it belongs")
        return out

    def match(e):
        return (m and (e.get("arxiv") or "") == m.group(1)) or (nt and norm_title(e.get("title")) == nt)
    if cov:
        n = next((e for e in cov.get("neighbours", []) if match(e)), None)
        if n:
            out["verdict"], out["neighbour"] = "neighbour", n
            out["notes"].append(f"not in the pool, but linked to {n['links']} included papers: "
                                + ", ".join(f"{v['paper_id']} ({v['direction']})" for v in n["via"]))
            return out
        for sv in cov.get("survey_refs", []):
            e = next((e for e in sv["refs"] if match(e)), None)
            if e:
                out["verdict"], out["survey_ref"] = "survey-ref", dict(e, survey_id=sv["survey_id"])
                out["notes"].append(f"cited by survey {sv['survey_id']} but never a candidate: its reference walk did not reach it")
                return out
    out["verdict"] = "not-seen"
    if not cov:
        out["notes"].append("no data/coverage.json yet: run `coverage` to check the citation neighbourhood")
    gaps = (cov or {}).get("gaps") or _coverage_gaps(rv)
    for z in gaps["zero_ref_walks"]:
        out["notes"].append(f"reference walk {z['run_id']} from {z['paper_id']} found 0 references ({z.get('limitations') or 'no reason'})")
    for t in gaps["skipped_terms"]:
        out["notes"].append(f"term {t['id']} {t['term']!r} was {t['status']}: {t.get('reason') or 'no reason'}")
    for f in (cov or {}).get("failed", []):
        out["notes"].append(f"S2 {f['direction']} edges of {f['paper_id']} failed ({f['status']}): rerun `coverage`")
    out["notes"].append("to bring it in: `paper <arXiv id>` then `screen --id <P-id> --decision included --reason ...`")
    return out


# --------------------------------------------------------------------------
# scope agreement + decisions log (meta.scope, meta.decisions)
# --------------------------------------------------------------------------

DECISION_KINDS = ("scope", "cap", "screen", "protocol", "kpi", "other")
SCOPE_REQUIRED_FROM = "2026-10-08"   # reviews created before the scope step existed are not warned
# an in item that admits "general ... or domain/task ..." (either order): general methods then compete with the
# review's own subject for the cap, and citation-ranked select favours them (llm-to-domain-slm scope v1)
_MIXED_SCOPE_ITEM = re.compile(r"\bgeneral(?:[- ]purpose)?\b[^;:]{0,30}?\bor\b[^;:]{0,30}?\b(?:domain|task)\S*"
                               r"|\b(?:domain|task)[- ](?:aware|specific|targeted)\b[^;:]{0,30}?\bor\b[^;:]{0,30}?\bgeneral\S*", re.I)


def _check_refs(rv: Review, refs: list[str]):
    for r in refs:
        tbl = {v: k for k, v in PREFIX.items()}.get(r[:1])
        if r.startswith("K"):
            if not any(k.get("id") == r for k in rv.meta.get("kpis") or []):
                raise SystemExit(f"unknown KPI {r}")
        elif tbl is None or tbl in LIST_TABLES or r not in rv.tables.get(tbl, {}):
            raise SystemExit(f"unknown record {r}")


def add_decision(rv: Review, decision: str, reason: str, kind: str = "other", refs: list[str] | None = None,
                 by: str = "user", date: str | None = None) -> dict:
    """Append a judgement call (scope, cap change, screening, protocol) with its reason to meta.decisions."""
    if kind not in DECISION_KINDS:
        raise SystemExit(f"kind must be one of {', '.join(DECISION_KINDS)}")
    if not (decision or "").strip() or not (reason or "").strip():
        raise SystemExit("a decision and a reason are required")
    if by not in ("user", "agent"):
        raise SystemExit("by must be user or agent")
    refs = [r.strip() for r in refs or [] if r.strip()]
    _check_refs(rv, refs)
    log = rv.meta.setdefault("decisions", [])
    d = {"id": f"DEC{len(log) + 1:03d}", "date": date or today(), "kind": kind, "decision": decision.strip(),
         "reason": reason.strip(), "refs": refs, "by": by}
    log.append(d)
    return d


def set_scope(rv: Review, include: list[str], exclude: list[str], protocol: list[str], notes: str | None,
              by: str = "user") -> dict:
    """Record the step-0 scope agreement (what is in, what is out, protocol rules) and log it as a decision."""
    if not include:
        raise SystemExit("scope needs at least one --in item")
    sc = {"in": list(include), "out": list(exclude), "protocol": list(protocol), "notes": notes, "agreed_on": today(), "by": by}
    rv.meta["scope"] = sc
    summary = "in: " + "; ".join(include) + (" | out: " + "; ".join(exclude) if exclude else "")
    add_decision(rv, f"scope set ({summary})", notes or "step-0 scope agreement", kind="scope", by=by)
    return sc


# --------------------------------------------------------------------------
# Screening against the scope (per paper, by a subagent; replaces batch screening by title)
# --------------------------------------------------------------------------

NO_IN = "matches no in item"
SCREEN_VERDICTS = ("on-topic", "off-topic", "needs-text")
SCREEN_KINDS = ("method", "dataset", "benchmark", "survey", "evaluation", "other")
_TEXT_ACCESS = re.compile(r"no readable (full )?text|abstract[- ]only|publisher[- ]only", re.I)
BOUNDARY_MIN = 5          # off-topic papers sharing an area no scope item names -> ask the user


def _fulltext_method(rv: Review, pid: str) -> str | None:
    f = rv.path / "cache" / "fulltext" / f"{pid}.meta.json"
    try:
        return json.loads(f.read_text()).get("method") if f.exists() else None
    except json.JSONDecodeError:
        return None


def _no_text_to_read(rv: Review, p: dict) -> bool:
    """`fulltext` ran and found only the abstract, and the record's abstract is empty or a stub (an author list)."""
    return _fulltext_method(rv, p["id"]) == "abstract-only" and len(str(p.get("abstract") or "").strip()) < 100


def screen_input(rv: Review, ids: list[str] | None = None, status: str = "candidate", batch: int = 150,
                 needs_text: bool = False) -> dict:
    """Write data/screening/input-NN.json packets (scope + papers) for the screening subagents."""
    sc = rv.meta.get("scope")
    if not sc or not sc.get("in"):
        raise SystemExit("no scope recorded: run `scope --in ... --out ...` first; screening cites scope items")
    P = rv.tables["papers"]
    if ids:
        sel = [P[i] for i in ids if i in P]
    elif needs_text:
        sel = [p for p in P.values() if p.get("status") in ("candidate", "included") and (p.get("screening") or {}).get("verdict") == "needs-text"
               and not _ts(p.get("manual_at")) > _ts(p["screening"].get("at"))]
        dead = [p["id"] for p in sel if p.get("status") == "candidate" and _no_text_to_read(rv, p)]
        sel = [p for p in sel if p["id"] not in dead]
        if dead and not sel:
            raise SystemExit(f"all {len(dead)} needs-text papers have no readable copy and no abstract ({_ids(dead)}): "
                             f"run merge-screening, which excludes them for text access")
    elif status == "included":       # the user already decided manual/seed/taxonomy inclusions (the "never screened" filter)
        sel = [p for p in P.values() if p.get("status") == "included" and not p.get("screening")
               and not p.get("manual") and not p.get("seed") and not p.get("taxonomy_source")]
    else:
        sel = [p for p in P.values() if p.get("status") == status and not (status == "candidate" and p.get("screening"))]
    if not sel:
        raise SystemExit("no papers to screen")
    links: dict[str, int] = {}
    for l in rv.tables["discovery_links"]:
        links[l["paper_id"]] = links.get(l["paper_id"], 0) + 1
    # area names parallel agents should reuse, so one boundary is not split across spellings
    known = list(dict.fromkeys([b["area"] for b in rv.meta.get("boundaries") or []]
                               + [t["name"] for t in (rv.meta.get("adjacent") or {}).get("topics") or []]))
    out_dir = rv.path / "data" / "screening"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = len(list(out_dir.glob("input-*.json")))
    files = []
    for k in range(0, len(sel), batch):
        papers = []
        for p in sel[k:k + batch]:
            m = _fulltext_method(rv, p["id"])
            rec = {"paper_id": p["id"], "title": p.get("title"), "year": p.get("year"), "venue": p.get("venue"),
                   "abstract": (p.get("abstract") or None) and str(p["abstract"])[:1200], "discovery_paths": links.get(p["id"], 0),
                   "full_text": m}
            t = rv.path / "cache" / "fulltext" / f"{p['id']}.txt"
            if m and m != "abstract-only" and t.exists():
                rec["text_path"] = str(t)        # the subagent reads it when the abstract leaves the scope question open
            papers.append(rec)
        n = stamp + len(files) + 1
        f = out_dir / f"input-{n:02d}.json"
        f.write_text(json.dumps({"purpose": rv.meta.get("purpose"), "scope": sc, "no_in": NO_IN, "known_areas": known,
                                 "decisions": [{"id": d["id"], "kind": d["kind"], "decision": d["decision"][:300]}
                                               for d in rv.meta.get("decisions") or [] if d["kind"] in ("scope", "screen", "protocol")],
                                 "papers": papers, "output_path": str(out_dir / f"batch-{n:02d}.json")}, indent=1, ensure_ascii=False))
        files.append(str(f))
    return {"papers": len(sel), "packets": files}


def _kind_in_scope(sc: dict, kind: str) -> bool:
    word = {"dataset": r"dataset", "benchmark": r"benchmark|dataset", "survey": r"survey"}.get(kind)
    return bool(word) and any(re.search(word, it, re.I) for it in sc.get("in") or [])


def _ts(iso, default: float = 0.0) -> float:
    """Epoch seconds of an ISO timestamp ('2026-10-08T12:00:00Z' or with microseconds); `default` when missing."""
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() if iso else default
    except ValueError:
        return default


def _batch_written(rv: Review, old: dict, src: str, written: float) -> float:
    """When the batch behind a paper's current verdict was written (records before v1.14.2 lack `batch_written`)."""
    if old.get("batch_written") is not None:
        return old["batch_written"]
    if old.get("source") == src:     # the same file: applied, unless it was rewritten after that merge
        return written if _ts(old.get("at")) >= int(written) else 0.0
    f = rv.path / str(old.get("source") or "")
    return f.stat().st_mtime if old.get("source") and f.exists() else 0.0


def merge_screening(rv: Review, rescreen: bool = False, batches: list[int] | None = None) -> dict:
    """Apply data/screening/batch-*.json verdicts. off-topic -> excluded (per-paper reason citing a scope item);
    on-topic -> stays candidate for `select`; needs-text -> candidate until `fulltext` + a with-text rescreen, then
    excluded for text access only if the text really could not be fetched. Contradictory verdicts are rejected.
    A batch already applied to a paper is not applied again, and a manual `screen` decision is kept unless the batch
    was written after it, so re-merging for a new batch never undoes later decisions. Included papers get their
    verdict recorded but keep their status; off-topic ones are reported for the user to decide (`screen`)."""
    sc = rv.meta.get("scope") or {}
    ins, outs = list(sc.get("in") or []), list(sc.get("out") or [])
    P = rv.tables["papers"]
    files = sorted((rv.path / "data" / "screening").glob("batch-*.json"))
    if batches:
        want = {f"batch-{n:02d}.json" for n in batches}
        files = [f for f in files if f.name in want]
        if len(files) < len(want):
            raise SystemExit(f"no data/screening/ file for: {', '.join(sorted(want - {f.name for f in files}))}")
    if not files:
        raise SystemExit("no data/screening/batch-*.json: run screen-input and the screening subagents first")
    rep = {"excluded": 0, "on_topic": 0, "needs_text": [], "text_access_excluded": [], "rejected": [], "skipped": [],
           "already_applied": 0, "kept_manual": [], "included_screened": 0, "included_off_topic": [], "included_needs_text": [],
           "check_kind": [], "undecided_boundaries": []}
    norm = lambda x: re.sub(r"\s+", " ", str(x or "")).strip().lower()
    in_n, out_n = {norm(i): i for i in ins}, {norm(o): o for o in outs}
    areas: dict[str, list[str]] = {}
    for f in files:
        src = f"data/screening/{f.name}"
        written = f.stat().st_mtime
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            rep["rejected"].append(f"{f.name}: invalid JSON ({e})")
            continue
        # a batch judges only the papers of its own input packet: one injected abstract cannot steer the verdicts of
        # other papers (security scan F2); batches with no packet file (hand-made, older reviews) are not checked
        pk = f.with_name(f.name.replace("batch-", "input-", 1))
        try:
            mine = {x.get("paper_id") for x in json.loads(pk.read_text()).get("papers") or []} if pk.exists() else None
        except (ValueError, AttributeError):
            mine = None
        for v in (data.get("verdicts") if isinstance(data, dict) else data) or []:
            pid, verdict = v.get("paper_id"), v.get("verdict")
            kind, ref, reason = v.get("kind") or "method", norm(v.get("scope_ref")), str(v.get("reason") or "").strip()
            if mine is not None and pid not in mine:
                rep["rejected"].append(f"{pid}: not in {pk.name}, the packet this batch answers")
                continue
            p = P.get(pid)
            if p is None:
                rep["rejected"].append(f"{pid}: unknown paper")
                continue
            old = p.get("screening") or {}
            if old and _batch_written(rv, old, src, written) >= written:
                rep["already_applied"] += 1       # the paper holds this batch's verdict or a newer one: re-merging changes nothing
                continue
            if str(p.get("status_reason") or "").startswith("manual:") and _ts(p.get("manual_at"), float("inf")) >= written:
                rep["kept_manual"].append(pid)    # a `screen` decision newer than this batch (or of unknown age) wins
                continue
            if p.get("status") == "excluded" and not rescreen:
                rep["skipped"].append(pid)
                continue
            if verdict not in SCREEN_VERDICTS or not reason:
                rep["rejected"].append(f"{pid}: verdict {verdict!r} or reason missing")
                continue
            if verdict == "on-topic" and ref not in in_n:
                rep["rejected"].append(f"{pid}: on-topic must cite a scope `in` item verbatim, got {v.get('scope_ref')!r}")
                continue
            if verdict == "off-topic" and ref not in out_n and ref != NO_IN:
                rep["rejected"].append(f"{pid}: off-topic must cite a scope `out` item verbatim or {NO_IN!r}, got {v.get('scope_ref')!r}")
                continue
            if verdict == "off-topic" and _kind_in_scope(sc, kind) and re.search(r"not a method|dataset paper|only a dataset|is (?:only |just |merely )?a survey(?! of)", reason, re.I):
                # P448: a dataset paper excluded for being a dataset while the scope includes datasets
                rep["rejected"].append(f"{pid}: excluded for being a {kind}, but the scope includes {kind}s - judge its topic")
                continue
            if verdict == "off-topic" and _kind_in_scope(sc, kind):
                rep.setdefault("check_kind", []).append(pid)      # off-topic dataset/survey: fine when its area is elsewhere
            scr = {"verdict": verdict, "kind": kind, "scope_ref": v.get("scope_ref"), "reason": reason, "area": v.get("area"),
                   "near": bool(v.get("near")),
                   "source": src, "batch_written": written, "at": now_iso()}
            p["screening"] = scr
            if p.get("status") == "included":     # recorded, never auto-excluded: the user decides with `screen`
                rep["included_screened"] += 1
                if verdict == "off-topic":
                    rep["included_off_topic"].append(pid)
                elif verdict == "needs-text":
                    rep["included_needs_text"].append(pid)
                continue
            if kind in ("dataset", "benchmark", "survey", "evaluation") and verdict != "off-topic" and p.get("paper_role", "method") == "method":
                p["paper_role"] = "benchmark" if kind == "dataset" else kind
                p["paper_role_reason"] = f"screening: {reason}"
            if verdict == "off-topic":
                p["status"], p["status_reason"], p["manual"] = "excluded", f"screening: {v.get('scope_ref')}: {reason}", True
                rep["excluded"] += 1
                if ref == NO_IN and v.get("near"):      # only areas next to the task are scope questions
                    areas.setdefault(norm(v.get("area")) or "(no area given)", []).append(pid)
            elif verdict == "on-topic":
                p["status"], p["status_reason"], p["manual"] = "candidate", f"screening: on-topic ({v.get('scope_ref')})", False
                rep["on_topic"] += 1
            else:
                m = _fulltext_method(rv, pid)
                if m == "abstract-only":
                    p["status"], p["manual"] = "excluded", True
                    p["status_reason"] = (f"screening: needs the full text to decide ({reason}); fulltext found no readable copy "
                                          "(arXiv, jina, PDF, open-access) - text access, not topic")
                    rep["text_access_excluded"].append(pid)
                else:
                    p["status"], p["status_reason"], p["manual"] = "candidate", f"screening: needs text ({reason})", False
                    rep["needs_text"].append(pid)
    # needs-text candidates that `fulltext` could not read and that have no abstract: nobody can judge their topic, so
    # they are excluded for text access here instead of going through another screening packet (table-qa 2026-10-29:
    # 170 such papers went through a second packet only to come back needs-text)
    for pid, p in P.items():
        if (p.get("status") == "candidate" and (p.get("screening") or {}).get("verdict") == "needs-text"
                and not _ts(p.get("manual_at")) > _ts(p["screening"].get("at")) and _no_text_to_read(rv, p)):
            p["status"], p["manual"] = "excluded", True
            p["status_reason"] = ("screening: needs the full text to decide; fulltext found no readable copy and the record has "
                                  "no abstract - text access, not topic")
            rep["text_access_excluded"].append(pid)
    decided = {norm(b["area"]) for b in rv.meta.get("boundaries") or [] if b.get("decision_id")}
    bounds = {norm(b["area"]): b for b in rv.meta.get("boundaries") or []}
    for area, pids in areas.items():
        b = bounds.setdefault(area, {"area": area, "paper_ids": [], "decision_id": None})
        b["paper_ids"] = sorted(set(b["paper_ids"]) | set(pids))
    rv.meta["boundaries"] = list(bounds.values())      # every no-in area, so small ones add up across merges
    rep["undecided_boundaries"] = [{"area": b["area"], "papers": len(b["paper_ids"])} for b in rv.meta["boundaries"]
                                   if not b.get("decision_id") and norm(b["area"]) not in decided and len(b["paper_ids"]) >= BOUNDARY_MIN]
    rv.derive()
    return rep


def decide_boundary(rv: Review, area: str, decision_id: str) -> dict:
    """Mark an undecided scope boundary (an area screening kept finding outside every scope item) as decided."""
    b = next((b for b in rv.meta.get("boundaries") or [] if b["area"] == re.sub(r"\s+", " ", area).strip().lower()), None)
    if b is None:
        raise SystemExit(f"no boundary {area!r}; see `validate` or meta.boundaries")
    b["decision_id"] = decision_id
    return b


def screening_warnings(rv: Review) -> list[str]:
    out, P = [], rv.tables["papers"]
    for b in rv.meta.get("boundaries") or []:
        if not b.get("decision_id") and len(b["paper_ids"]) >= BOUNDARY_MIN:
            out.append(f"scope boundary undecided: screening put {len(b['paper_ids'])} papers on '{b['area']}', which no scope item "
                       f"names - ask the user, then `decide --kind scope --boundary \"{b['area']}\" ...` (and `scope` if it widens)")
    if any(p.get("screening") for p in P.values()):
        raw = sorted(pid for pid, p in P.items() if p.get("status") == "included" and not p.get("screening")
                     and not p.get("manual") and not p.get("seed") and not p.get("taxonomy_source"))
        if raw:
            out.append(f"{len(raw)} included papers were never screened against the scope ({', '.join(raw[:8])}"
                       f"{' ...' if len(raw) > 8 else ''}): `screen-input --status included` -> subagent -> `merge-screening`")
    off = sorted(pid for pid, p in P.items() if p.get("status") == "included" and (p.get("screening") or {}).get("verdict") == "off-topic"
                 and not _ts(p.get("manual_at")) > _ts(p["screening"].get("at")))
    if off:
        out.append(f"{len(off)} included papers were screened off-topic ({', '.join(off[:8])}{' ...' if len(off) > 8 else ''}): read "
                   "papers.screening.reason, then `screen --decision excluded` or keep them with `screen --decision included`")
    bad = sorted(pid for pid in P if _fulltext_method(rv, pid) == "jina-oa" and rv.fulltext_path(pid).exists()
                 and not _looks_like_paper(rv.fulltext_path(pid).read_text(errors="replace")))
    if bad:
        out.append(f"{len(bad)} cached jina-oa texts look like a paywall or landing page, not the paper ({', '.join(bad[:8])}"
                   f"{' ...' if len(bad) > 8 else ''}): `fulltext --force` them, then rescreen")
    no_try = [pid for pid, p in P.items() if p.get("status") == "excluded" and _TEXT_ACCESS.search(str(p.get("status_reason") or ""))
              and _fulltext_method(rv, pid) is None]
    if no_try:
        out.append(f"{len(no_try)} papers excluded for text access without a `fulltext` attempt ({', '.join(sorted(no_try)[:8])}"
                   f"{' ...' if len(no_try) > 8 else ''}): run `fulltext` on them, then rescreen")
    return out


# --------------------------------------------------------------------------
# Adjacent topics: the screened-out landscape around the scope
# --------------------------------------------------------------------------

ADJ_GROUPS = ("in_scope", "unrelated")    # reserved buckets beside the topics: on-topic but excluded; not adjacent at all


def _cit(p: dict):
    c = p.get("citations")
    return c.get("value") if is_evidence(c) else c


_ADJ_AUTO_IN_SCOPE = re.compile(r"year window|over cap|seminal", re.I)


def _adjacent_auto_in_scope(p: dict) -> bool:
    """An excluded paper that screening found on-topic and only the cap, the year window or the seminal quota kept out:
    it is in_scope by rule, so the adjacent-topics packet leaves it out (table-qa 2026-10-29: 3,400 excluded papers
    made a 1.1 MB packet). Text-access and unscreened exclusions stay in the packet: their topic is unknown."""
    return (p.get("status") == "excluded" and (p.get("screening") or {}).get("verdict") == "on-topic"
            and bool(_ADJ_AUTO_IN_SCOPE.search(str(p.get("status_reason") or ""))))


def adjacent_input(rv: Review, abstract_chars: int = 300) -> dict:
    """Packet for the adjacent-topics subagent: the scope, the decisions and every excluded paper that needs a judgement
    (title, abstract head); papers in_scope by rule are only counted (`auto_in_scope`), and merge-adjacent adds them."""
    exc = [p for p in rv.tables["papers"].values() if p.get("status") == "excluded"]
    if not exc:
        raise SystemExit("no excluded papers: nothing to group")
    auto = sorted(p["id"] for p in exc if _adjacent_auto_in_scope(p))
    exc = [p for p in exc if not _adjacent_auto_in_scope(p)]
    exc.sort(key=lambda p: (-(_cit(p) or 0), p["id"]))
    papers = [{"paper_id": p["id"], "title": p.get("title"), "year": p.get("year"), "citations": _cit(p),
               "excluded_because": re.sub(r"^manual: ", "", str(p.get("status_reason") or ""))[:160],
               "abstract": (str(p.get("abstract") or "")[:abstract_chars] or None)} for p in exc]
    return {"purpose": rv.meta.get("purpose"), "scope": rv.meta.get("scope"),
            "decisions": [{"id": d["id"], "kind": d["kind"], "decision": d["decision"][:300]} for d in rv.meta.get("decisions") or []],
            "papers": papers, "auto_in_scope": {"count": len(auto), "rule": "screened on-topic, excluded only by cap, year window "
                                                "or seminal quota; merge-adjacent adds them to in_scope - do not list them"},
            "output_path": str(rv.path / "data" / "adjacent" / "topics.json")}


def merge_adjacent(rv: Review) -> dict:
    """Import data/adjacent/topics.json into meta.adjacent (topics with members, plus the in_scope / unrelated buckets)."""
    f = rv.path / "data" / "adjacent" / "topics.json"
    if not f.exists():
        raise SystemExit(f"{f} not found; the adjacent-topics subagent writes it")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{f}: invalid JSON ({e}); nothing changed")
    items = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise SystemExit(f'{f}: expected {{"topics": [{{"name": ..., "paper_ids": [...]}}], "in_scope": [...], "unrelated": [...]}}; nothing changed')
    P = rv.tables["papers"]
    exc = {pid for pid, p in P.items() if p.get("status") == "excluded"}
    rep = {"topics": 0, "assigned": 0, "in_scope": 0, "unrelated": 0, "unassigned": [], "warnings": []}
    seen: dict[str, str] = {}

    def take(pid, where):
        if pid not in P:
            rep["warnings"].append(f"{where}: unknown paper {pid} - dropped")
        elif pid not in exc:
            rep["warnings"].append(f"{where}: {pid} is {P[pid].get('status')}, not excluded - dropped")
        elif pid in seen:
            rep["warnings"].append(f"{pid} is in {seen[pid]} and {where} - kept in {seen[pid]}")
        else:
            seen[pid] = where
            return True
        return False

    topics = []
    for it in items:
        name = str((it or {}).get("name") or "").strip() if isinstance(it, dict) else ""
        if not name:
            rep["warnings"].append("topic without a name skipped")
            continue
        tid = f"A{len(topics) + 1:02d}"
        ids = [pid for pid in dict.fromkeys(it.get("paper_ids") or []) if take(pid, tid)]
        if not ids:
            rep["warnings"].append(f"{name}: no valid members - skipped")
            continue
        refs = [r for r in it.get("decision_refs") or [] if any(d["id"] == r for d in rv.meta.get("decisions") or [])]
        topics.append({"id": tid, "name": name, "definition": it.get("definition"), "relation": it.get("relation"),
                       "scope_out": it.get("scope_out"), "decision_refs": refs, "paper_ids": ids})
    if not topics:
        raise SystemExit(f"{f}: no topic with valid members; nothing changed")
    groups = {g: [pid for pid in dict.fromkeys((data.get(g) or []) if isinstance(data.get(g), list) else []) if take(pid, g)]
              for g in ADJ_GROUPS}
    # papers adjacent-input left out by rule: in_scope unless the writer placed them itself
    auto = sorted(pid for pid in exc if pid not in seen and _adjacent_auto_in_scope(P[pid]))
    for pid in auto:
        seen[pid] = "in_scope"
    groups["in_scope"] += auto
    rep["auto_in_scope"] = len(auto)
    rep["topics"], rep["assigned"] = len(topics), sum(len(t["paper_ids"]) for t in topics)
    rep["in_scope"], rep["unrelated"] = len(groups["in_scope"]), len(groups["unrelated"])
    rep["unassigned"] = sorted(exc - set(seen))
    # exclusions for access, cap, window, duplication, paper kind (tutorial, workshop report) or invalid results are not
    # topic calls (MDE: "no full text could be fetched", "journal extension ..."; retrieval-confidence: "no accessible
    # full text", "tutorial/book summary", "workshop proposal", "simulated example values" were flagged as misscreens)
    topical = lambda pid: not re.search(r"year window|over cap|seminal|no readable|abstract[- ]only|publisher[- ]only|text access"
                                        r"|no (?:accessible )?(?:full )?text|full text could not|behind a publisher|journal extension"
                                        r"|duplicate|one record per method|no method or evaluation|tutorial|workshop proposal"
                                        r"|special[- ]issue introduction|simulated"
                                        # llm-to-domain-slm: a survey quota (DEC009), benchmark and dataset papers kept
                                        # as records (DEC020), and screener verdicts that needed the full text
                                        r"|general survey|general review|position paper|benchmark-only|kept as a (?:dataset|benchmark) record"
                                        r"|types it as a dataset|needs the full text|no experiments",
                                        str(P[pid].get("status_reason") or ""), re.I)
    rep["possible_misscreens"] = [pid for pid in groups["in_scope"] if topical(pid)]
    rv.meta["adjacent"] = {"generated_at": now_iso(), "source": "data/adjacent/topics.json", "topics": topics, **groups,
                           "possible_misscreens": rep["possible_misscreens"]}
    return rep


def adjacent_warnings(rv: Review) -> list[str]:
    adj = rv.meta.get("adjacent")
    if not adj:
        return []
    P, out = rv.tables["papers"], []
    placed = {pid for t in adj.get("topics") or [] for pid in t.get("paper_ids") or []} | {pid for g in ADJ_GROUPS for pid in adj.get(g) or []}
    new = [pid for pid, p in P.items() if p.get("status") == "excluded" and pid not in placed]
    if new:
        out.append(f"adjacent topics: {len(new)} excluded papers are in no topic ({', '.join(sorted(new)[:8])}"
                   f"{' ...' if len(new) > 8 else ''}); rerun adjacent-input -> subagent -> merge-adjacent")
    reviewed = lambda p: max(_ts((p.get("screening") or {}).get("at")), _ts(p.get("manual_at"))) > _ts(adj.get("generated_at"))
    mis = [pid for pid in adj.get("possible_misscreens") or [] if P.get(pid, {}).get("status") == "excluded"
           and not reviewed(P[pid])]       # a rescreen or a `screen` decision after the adjacent run settles it
    if mis:
        out.append(f"adjacent topics: {len(mis)} papers fit the scope but were excluded as off-topic ({', '.join(mis[:8])}"
                   f"{' ...' if len(mis) > 8 else ''}): rescreen them (`screen-input --ids ...` -> subagent -> `merge-screening --rescreen`)")
    moved = sorted(pid for pid in placed if pid in P and P[pid].get("status") != "excluded")
    if moved:
        out.append(f"adjacent topics: {len(moved)} grouped papers are no longer excluded ({', '.join(moved[:8])}); the hub hides them")
    return out


# --------------------------------------------------------------------------
# BibTeX
# --------------------------------------------------------------------------

_BIB_STOP = {"a", "an", "the", "on", "of", "towards", "toward", "for", "in", "to", "and", "with"}


def _bib_esc(s: str) -> str:
    s = str(s).replace("\\", "\0")
    for c in "&%$#_{}":
        s = s.replace(c, "\\" + c)
    return s.replace("~", "\1").replace("^", "\2").replace("\0", r"\textbackslash{}") \
        .replace("\1", r"\textasciitilde{}").replace("\2", r"\textasciicircum{}")


def _bib_url(s: str) -> str:
    """A metadata URL or DOI for a {...} BibTeX field: braces and backslashes percent-encoded, so a '}' cannot end the
    field and add one such as note={\\input{...}} (security scan F12). The URL still resolves."""
    return str(s).replace("{", "%7B").replace("}", "%7D").replace("\\", "%5C")      # '%' stays: URLs keep their escapes


def _ascii_word(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def _bib_authors(authors: list) -> list[str]:
    """Authors once each: arXiv + OpenAlex merges repeat a name as 'Kang, Bingyi' / 'Bingyi Kang' / 'Xiao-Gang Xu'."""
    seen, out = set(), []
    for a in authors or []:
        k = "".join(sorted(_ascii_word(a)))
        if a and k not in seen:
            seen.add(k)
            out.append(a)
    return out


def _bib_last(a: str) -> str:
    return _ascii_word(a.split(",")[0] if "," in a else a.split()[-1])


def bibtex(rv: Review) -> str:
    """BibTeX for the included papers: @inproceedings / @article when a published venue is verified or
    author-reported, else @misc with the arXiv eprint. Keys are <lastname><year><firstword>, made unique with a/b/..."""
    used, out = {}, []
    for p in sorted((p for p in rv.tables["papers"].values() if p.get("status") == "included"), key=lambda p: p["id"]):
        # titles and names as the hub shows them ('$π^3$' -> 'π³'): _bib_esc would print TeX as literal characters,
        # and the hub's Download BibTeX derives the same keys from the same text
        p = {**p, "title": tex_to_text(p.get("title")), "authors": [tex_to_text(a) for a in p.get("authors") or []]}
        authors = _bib_authors(p.get("authors"))
        last = _bib_last(authors[0]) if authors else "anon"
        words = [_ascii_word(w) for w in re.split(r"[\s:\-]+", p.get("title") or "")]
        first = next((w for w in words if w and w not in _BIB_STOP), "untitled")
        base = f"{last or 'anon'}{p.get('year') or ''}{first}"
        n = used.get(base, 0)
        used[base] = n + 1
        key = base + ("" if n == 0 else "abcdefghijklmnopqrstuvwxyz"[(n - 1) % 26])
        ven = p.get("published_venue")
        v = ven.get("value") if is_evidence(ven) and ven.get("status") in ("verified", "author-reported") else None
        ids = p.get("ids") or {}
        fields = [("title", "{" + _bib_esc(p.get("title") or "") + "}"), ("author", " and ".join(_bib_esc(a) for a in authors) or None),
                  ("year", p.get("year"))]
        if v and v.get("name"):
            etype = "article" if v.get("kind") == "journal" else "inproceedings"
            fields.append(("journal" if etype == "article" else "booktitle", _bib_esc(v["name"].split("/")[0].strip())))
        else:
            etype = "misc"
        if ids.get("arxiv"):
            fields += [("eprint", ids["arxiv"]), ("archivePrefix", "arXiv")]
            if p.get("categories"):
                fields.append(("primaryClass", p["categories"][0]))
        if ids.get("doi") and not str(ids["doi"]).lower().startswith("10.48550/"):
            fields.append(("doi", _bib_url(ids["doi"])))
        if p.get("url"):
            fields.append(("url", _bib_url(p["url"])))
        body = ",\n".join(f"  {k} = {{{val}}}" for k, val in fields if val not in (None, ""))
        out.append(f"@{etype}{{{key},\n{body}\n}}\n")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _review_path(arg: str) -> Path:
    p = Path(arg)
    if not p.exists():
        alt = Path.home() / "code" / "literatureReview" / "reviews" / arg
        if alt.exists():
            p = alt
    if not (p / "meta.json").exists():
        raise SystemExit(f"no review at {p}; run `init` first")
    return p


def _open_review(arg: str) -> Review:
    return Review.load(_review_path(arg))


# --------------------------------------------------------------------------
# one writer per review
# --------------------------------------------------------------------------
# Every command loads all tables, works, then writes whole files back, so two writers running at once lose the
# first one's changes (a background `enrich` restored a paper a concurrent `screen` had just excluded). Writers take
# an exclusive flock for their whole run and a second writer fails fast: agents run commands under tool timeouts,
# where a silent wait looks like a hang and may be killed anyway, while an immediate error names the command to
# wait for. Read-only commands take no lock. The lock lives in cache/ (gitignored) and is never deleted.

def lock_path(review: Path) -> Path:
    return Path(review) / "cache" / "litrev.lock"


def acquire_lock(review: Path, cmd: str):
    """Exclusive non-blocking lock on the review; released when the returned handle is closed or the process exits."""
    try:
        import fcntl
    except ImportError:          # no flock on this platform: run unlocked rather than refuse
        return None
    lp = lock_path(review)
    lp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lp, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown command"
        fh.close()
        raise SystemExit(f"review {review} is locked by another litrev command that writes data ({holder}). "
                         f"Wait for it to finish, then rerun `{cmd}`; running both would let the last writer erase the other's changes. "
                         f"Read-only commands (show, list, validate, details-status, check-details, kpi --suggest/--list, rank-papers) still work.")
    fh.seek(0)
    fh.truncate()
    fh.write(f"{cmd} pid {os.getpid()} since {now_iso()}")
    fh.flush()
    return fh


def is_read_only(a) -> bool:
    """Commands that never write the review's tables (they take no lock and do not save)."""
    if a.cmd in ("show", "list", "check-details", "details-status", "validate", "cluster-input", "explainer-input",
                 "synthesis-input", "limitations-input", "emphasis-input", "dataset-info", "adjacent-input", "screen-input",
                 "presentation-input", "script-input", "check-script", "report-input"):
        return True
    if a.cmd == "kpi":
        return bool(a.suggest or a.list)
    if a.cmd == "rank-papers":
        return a.top is None
    if a.cmd == "select":
        return a.preview is not None
    if a.cmd == "refresh":
        return bool(a.dry_run)
    if a.cmd in ("why-missing", "status"):
        return True
    if a.cmd == "scope":
        return not (a.include or a.exclude or a.protocol or a.notes)
    if a.cmd == "decide":
        return bool(a.list)
    return False


# --------------------------------------------------------------------------
# status: what is done, missing or stale, and the next command
# --------------------------------------------------------------------------

def details_hashes(rv: Review) -> dict[str, str]:
    ddir = rv.path / "data" / "details"
    return {f.stem: hashlib.sha1(f.read_bytes()).hexdigest() for f in sorted(ddir.glob("*.json"))} if ddir.exists() else {}


def _ids(xs: list[str], n: int = 8) -> str:
    return ", ".join(xs[:n]) + (f" ... (+{len(xs) - n})" if len(xs) > n else "")


def review_status(rv: Review) -> dict:
    """One row per pipeline step: state done / todo / stale / warn / unknown, a detail and the next command. Read-only.
    Content checks where the data shows it (missing files, clusters vs the ranking); otherwise meta.step_log, the time
    each data-writing command last ran (reviews from before the log show `unknown`)."""
    P, T = rv.tables["papers"], rv.tables
    log = rv.meta.get("step_log") or {}
    inc = [p for p in sorted(P.values(), key=lambda p: p["id"]) if p.get("status") == "included"]
    rows: list[dict] = []

    def row(step, key, state, detail, nxt=""):
        rows.append({"step": step, "key": key, "state": state, "detail": detail, "next": nxt})

    def newer(a: str, *bs: str) -> list[str]:
        """Commands in bs that ran after a (a never ran -> [])."""
        return [b for b in bs if a in log and b in log and _ts(log[b]) > _ts(log[a])]

    # 0
    sc = rv.meta.get("scope") or {}
    row(0, "scope", "done" if sc.get("in") else "todo", f"agreed {sc.get('agreed_on')}" if sc.get("in") else "no scope agreement",
        "" if sc.get("in") else 'scope --in "..." --out "..." --protocol "..." (ask the user first)')
    tdir = rv.path / "data" / "seed_taxonomy"
    n_tax = len(list(tdir.glob("*.json"))) if tdir.exists() else 0
    row(0, "taxonomy", "done" if n_tax else "todo", f"{n_tax} survey taxonomy file(s)", "" if n_tax else "survey search -> taxonomy subagent")
    # 1
    pend = [t["id"] for t in T["search_terms"].values() if t.get("status") == "pending"]
    row(1, "search", "todo" if pend or not T["search_terms"] else "done",
        f"{len(pend)} pending term(s): {_ids(pend)}" if pend else f"{len(T['search_terms'])} term(s)",
        "search --term ..." if pend or not T["search_terms"] else "")
    uns = sorted(p["id"] for p in P.values() if p.get("status") == "candidate" and not p.get("screening"))
    ntx = sorted(p["id"] for p in P.values() if p.get("status") in ("candidate", "included")
                 and (p.get("screening") or {}).get("verdict") == "needs-text" and not _ts(p.get("manual_at")) > _ts(p["screening"].get("at")))
    row(1, "screen", "todo" if uns or ntx else "done",
        f"{len(uns)} unscreened candidate(s), {len(ntx)} needing text" if uns or ntx else "every candidate screened",
        ("screen-input -> screening subagents -> merge-screening" if uns else "fulltext <ids> -> screen-input --needs-text -> merge-screening")
        if uns or ntx else "")
    after = newer("select", "merge-screening", "screen", "coverage", "snowball", "refresh", "search")
    row(1, "select", "stale" if after else ("done" if "select" in log else ("todo" if not inc else "unknown")),
        f"{', '.join(after)} ran after the last select" if after else f"{len(inc)} included", "select" if after or not inc else "")
    front, deep, _ = snowball_frontier(rv)
    s2f = s2_failed_walks(rv)
    row(1, "snowball", "todo" if front or s2f else "done",
        (f"{len(front)} included walk-found paper(s) not forward-walked: {_ids(front)}" if front
         else ("converged" + (f" (max hop reached: {_ids(deep)})" if deep else "")))
        + (f"; {len(s2f)} walk(s) lost their Semantic Scholar supplement (HTTP 429): {_ids(s2f)}" if s2f else ""),
        "snowball -> screen -> select" if front or s2f else "")
    cov = rv.path / "data" / "coverage.json"
    # not select (coverage -> screen -> select is the intended order); search/refresh add papers no walk has covered
    after = newer("coverage", "snowball", "search", "refresh")
    row(1, "coverage", "todo" if not cov.exists() else ("stale" if after else "done"),
        "never run" if not cov.exists() else (f"{', '.join(after)} ran after it" if after else "run"),
        "coverage -> screen -> select" if not cov.exists() or after else "")
    # 2-3
    for step, key, tabs in ((2, "tasks", ("tasks",)), (3, "datasets", ("datasets", "benchmarks", "metrics"))):
        empty = [t for t in tabs if not T[t]]
        row(step, key, "todo" if empty else "done", f"empty: {', '.join(empty)}" if empty else
            ", ".join(f"{len(T[t])} {t}" for t in tabs), f"step {step} (see SKILL.md)" if empty else "")
    cand = sorted(r["id"] for t in ("datasets", "metrics") for r in T[t].values() if r.get("status") == "candidate")
    if cand:
        row(3, "candidates", "warn", f"{len(cand)} candidate dataset/metric record(s): {_ids(cand)}", "define, fold or exclude each")
    # 4
    need = [p for p in inc if needs_details(p)]
    oa = [p["id"] for p in inc if not (p.get("ids") or {}).get("openalex")]
    if oa:   # OpenAlex may simply lack the record: a retry costs 10 credits a title and is not a pipeline gap
        row(4, "openalex", "warn", f"{len(oa)} included paper(s) without an OpenAlex record (no venue/citation data): {_ids(oa)}",
            f"paper {' '.join(oa[:8])} (retries the lookup)")
    for key, miss, cmd in (("fulltext", [p["id"] for p in need if not rv.fulltext_path(p["id"]).exists()], "fulltext included"),
                           ("enrich", [p["id"] for p in need if p.get("code") is None], "enrich included")):
        row(4, key, "todo" if miss else "done", f"{len(miss)} included paper(s) without it: {_ids(miss)}" if miss else
            f"{len(need)} done", cmd if miss else "")
    # 5
    ds = details_status(rv, quotes=False)
    bad = ds["missing"] + ds["invalid"]
    row(5, "details", "todo" if bad else "done", f"{len(ds['missing'])} missing, {len(ds['invalid'])} invalid: {_ids(bad)}"
        if bad else f"{len(ds['ok'])} details file(s)", "step-5 subagents for those papers -> details-status" if bad else "")
    ddir = rv.path / "data" / "details"
    files = [ddir / f"{pid}.json" for pid in ds["ok"]]
    if not files:
        row(5, "merge-details", "todo" if need else "done", "nothing to merge yet" if need else "no papers need details", "")
    elif "details_merged" not in rv.meta:
        if "init" in log:        # born with the step log (v1.19.0+): nothing merged yet
            row(5, "merge-details", "todo", f"{len(files)} details file(s) not merged yet", "merge-details")
        else:
            row(5, "merge-details", "unknown", "merged before the step log existed, or never", "merge-details (to start tracking)")
    else:
        merged, now = rv.meta["details_merged"], details_hashes(rv)
        late = sorted(f.stem for f in files if merged.get(f.stem) != now.get(f.stem))
        row(5, "merge-details", "stale" if late else "done", f"{len(late)} file(s) changed after the last merge: {_ids(late)}"
            if late else f"merged {log.get('merge-details', '')}".strip(), "merge-details" if late else "")
    # 6
    K = rv.meta.get("kpis") or []
    row(6, "kpi", "done" if K else "todo", f"{len(K)} KPI(s)" if K else "no KPIs", "" if K else "kpi --suggest -> kpi --add ...")
    C, X = T["clusters"], T["explainers"]
    if K:
        rp = rank_papers(rv)
        top = [r["paper_id"] for r in rp["ranked"]]
        inc_ids = {p["id"] for p in inc}
        in_c = {pid for c in C.values() for pid in c.get("paper_ids") or []}
        out_c, gone = [pid for pid in top if pid not in in_c], sorted(pid for pid in in_c if pid not in inc_ids)
        if not C:
            row(6, "clusters", "todo", f"{len(top)} ranked paper(s), no clusters", "cluster-input -> subagent -> merge-clusters")
        else:
            detail = "; ".join(x for x in (f"ranked but in no cluster: {_ids(out_c)}" if out_c else "",
                                           f"clustered but not included: {_ids(gone)}" if gone else "") if x)
            row(6, "clusters", "stale" if detail else "done", detail or f"{len(C)} cluster(s) cover the top {len(top)}",
                "cluster-input -> subagent -> merge-clusters" if detail else "")
    else:
        row(6, "clusters", "todo", "needs KPIs first", "")
    by_c = {x.get("cluster_id"): x for x in X.values()}
    no_x = sorted(cid for cid in C if cid not in by_c)
    wrong = sorted(cid for cid, c in C.items() if cid in by_c and by_c[cid].get("paper_id") != c.get("winner_paper_id"))
    orphan = sorted(cid for cid in by_c if cid not in C)
    no_crit = sorted(cid for cid, x in by_c.items() if cid in C and cid not in wrong
                     and not (x.get("declared_limitations") and x.get("weak_points")))
    if not C:
        row(6, "explainers", "todo", "needs clusters first", "")
    else:
        detail = "; ".join(x for x in (f"no explainer: {_ids(no_x)}" if no_x else "",
                                       f"explains a paper that is no longer the winner: {_ids(wrong)}" if wrong else "",
                                       f"cluster gone: {_ids(orphan)}" if orphan else "",
                                       f"no critique: {_ids(no_crit)}" if no_crit else "") if x)
        row(6, "explainers", "todo" if no_x and not (wrong or orphan or no_crit) else ("stale" if detail else "done"),
            detail or f"{len(X)} explainer(s)",
            ("explainer-input C### -> slide subagent + critique subagent -> merge-explainers" if no_x or wrong or orphan
             else "explainer-input C### -> critique subagent -> merge-explainers") if detail else "")
    syn = rv.meta.get("synthesis")
    late = X and syn and "merge-explainers" in log and _ts(log["merge-explainers"]) > _ts(syn.get("merged_at"))
    row(6, "synthesis", "todo" if X and not syn else ("stale" if late else ("done" if syn else "todo")),
        "merge-explainers ran after it" if late else (f"merged {syn.get('merged_at')}" if syn else "none"),
        "synthesis-input -> subagent -> merge-synthesis" if (X and not syn) or late else "")
    lim = rv.meta.get("limitations")
    ml = _missing_member_limits(rv) if X else []
    lim_late = X and lim and "merge-explainers" in log and _ts(log["merge-explainers"]) > _ts(lim.get("merged_at"))
    row(6, "limitations", "todo" if X and not lim else ("stale" if lim_late or (lim and ml) else ("done" if lim else "todo")),
        "; ".join(x for x in ("merge-explainers ran after it" if lim_late else "",
                              f"members with no stated limitations: {_ids(ml)}" if ml else "") if x)
        or (f"merged {lim.get('merged_at')}" if lim else "none"),
        ("explainer-input C### (fresh packets name each member's text and output) -> member-limitations subagent per member -> merge-explainers -> " if ml else "")
        + "limitations-input -> subagent -> merge-limitations" if (X and not lim) or lim_late or ml else "")
    if X:
        miss = [sid for sid, _, _, item in _emphasis_slots(rv) if "emphasis" not in item]
        row(6, "emphasis", "todo" if miss else "done",
            f"{len(miss)} long text(s) with no highlights: {_ids(miss)}" if miss else "every long text has its highlights",
            "emphasis-input -> subagent -> merge-emphasis" if miss else "")
    # 7
    errs, warns = validate_review(rv)
    row(7, "validate", "warn" if errs or warns else "done", f"{len(errs)} error(s), {len(warns)} warning(s)",
        "validate --strict" if errs or warns else "")
    aw = adjacent_warnings(rv)
    row(7, "adjacent", "todo" if not rv.meta.get("adjacent") else ("stale" if aw else "done"),
        "; ".join(aw) if aw else ("never run" if not rv.meta.get("adjacent") else "every excluded paper placed"),
        "adjacent-input -> subagent -> merge-adjacent" if aw or not rv.meta.get("adjacent") else "")
    rpt = rv.meta.get("report")
    if syn or rpt:          # the ten sections of the hub's Report tab (and of the video)
        rlate = [c for c in ("merge-clusters", "merge-explainers", "merge-synthesis", "merge-limitations", "kpi")
                 if rpt and c in log and _ts(log[c]) > _ts(rpt.get("merged_at"))]
        rw = [w for w in report_warnings(rv) if rpt]
        row(7, "report", "todo" if not rpt else ("stale" if rlate or rw else "done"),
            "no review report (the hub's Report tab shows only the tables)" if not rpt else
            "; ".join(([f"{', '.join(rlate)} ran after it"] if rlate else []) + rw) if rlate or rw else f"merged {rpt.get('merged_at')}",
            "report-input -> subagent -> merge-report" if not rpt or rlate or rw else "")
    hub = rv.path / "rendered" / "hub.html"
    # `video` writes only the MP4, its captions, the deck and video.json, which no rendered file shows (the hub's
    # Presentation tab shows the plan, the script and the audio): a rebuilt video leaves render and publish done
    writes = {c: t for c, t in log.items() if c not in ("render", "video") and not c.startswith("set-meta")}
    after = sorted((c for c, t in writes.items() if "render" in log and _ts(t) > _ts(log["render"])), key=lambda c: writes[c])
    if not hub.exists():
        row(7, "render", "todo", "no rendered/hub.html", "render")
    elif hub.stat().st_size > HUB_LIMIT_BYTES:     # a hub from an older skill, or a render that exited 1
        row(7, "render", "stale", f"hub.html is {hub.stat().st_size / 1e6:.2f} MB, over the {HUB_LIMIT_BYTES / 1e6:.0f} MB "
            "artifact limit", "render (it trims the hub to fit, or names the parts that do not fit)")
    elif "render" not in log:
        row(7, "render", "unknown", "hub exists; rendered before the step log existed", "render (to start tracking)")
    else:
        row(7, "render", "stale" if after else "done", f"{', '.join(after)} ran after the last render" if after
            else f"rendered {log['render']}; hub {hub.stat().st_size / 1e6:.1f} of {HUB_LIMIT_BYTES / 1e6:.0f} MB", "render" if after else "")
    hu = rv.meta.get("hub_url")
    pub_at = log.get("set-meta:hub_url")      # outputs.md renders again after it, so renders don't count
    late = sorted((c for c, t in writes.items() if pub_at and _ts(t) > _ts(pub_at)), key=lambda c: writes[c])
    row(7, "publish", "todo" if not hu else ("stale" if late else "done"),
        "no hub_url" if not hu else (f"{', '.join(late)} ran after the last publish record: {hu}" if late else hu),
        "publish rendered/hub.html as the Artifact, then set-meta --field hub_url" if not hu or late else "")
    _pres_status_rows(rv, row, log)      # 8: only when the person opted in to the presentation
    nxt = next((r for r in rows if r["state"] in ("todo", "stale")), None)
    return {"rows": rows, "next": nxt, "skill_version": SKILL_VERSION}


def check_json(path: str) -> int:
    """Parse one JSON file and say where it breaks: subagents check the files they write with this (app sessions
    deny python3 -c, grep patterns and plutil). Prints {ok, path, records|error}; exit 1 when it does not parse."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError as e:
        print(json.dumps({"ok": False, "path": path, "error": f"cannot read: {e.strerror}"}))
        return 1
    except ValueError as e:
        print(json.dumps({"ok": False, "path": path, "error": f"line {e.lineno} column {e.colno}: {e.msg}"}))
        return 1
    n = len(data) if isinstance(data, (list, dict)) else 1
    print(json.dumps({"ok": True, "path": path, "records": n}))
    return 0


# (command, argument) of every option that names a file or folder. In a Litrev App session (LITREV_APP_REVIEW = its
# review folder) these and --review must stay inside that folder: the app allows `Bash(python3 litrev.py *)`, so a
# prompt-injected session could otherwise import any JSON file the user can read (security scan F1, 2026-10-16).
APP_PATH_ARGS = (("init", "path"), ("check-json", "path"), ("import", "file"))


def _app_guard(a) -> None:
    """Refuse, in an app session, `init` and every review or path argument outside the session's review. Unset
    LITREV_APP_REVIEW (skill mode) changes nothing."""
    root = os.environ.get("LITREV_APP_REVIEW")
    if not root:
        return
    root = Path(root).resolve()
    # the app makes its reviews itself, never through a session; a session installs nothing (voice-setup runs pip)
    bad = a.cmd if a.cmd in ("init", "voice-setup") else None
    if not bad and getattr(a, "review", None) is not None and _review_path(a.review).resolve() != root:
        bad = f"--review {a.review}"
    for cmd, dest in APP_PATH_ARGS:
        if not bad and a.cmd == cmd and not Path(getattr(a, dest)).resolve().is_relative_to(root):
            bad = f"{cmd} {getattr(a, dest)}"
    if bad:
        raise SystemExit(f"LITREV_APP_REVIEW: {bad} is outside this app session's review {root}")


def main(argv=None):
    a = build_parser().parse_args(argv)
    _app_guard(a)
    lock = None
    if a.cmd == "check-json":
        return check_json(a.path)
    if a.cmd not in ("init", "pwc-index", "voice-setup") and not is_read_only(a):
        lock = acquire_lock(_review_path(a.review), a.cmd)
    try:
        return _dispatch(a)
    finally:
        if lock is not None:
            lock.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="litrev.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a review folder")
    s.add_argument("--path", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--purpose", required=True)
    s.add_argument("--seed", required=True, help="concept or paper title/arXiv id")
    s.add_argument("--max-papers", type=int, default=DEFAULT_MAX_PAPERS)
    s.add_argument("--years", type=int, default=3)
    s.add_argument("--seminal-citations", type=int, default=1000)
    s.add_argument("--max-seminal", type=int, default=10, help="max papers admitted via the seminal exemption")
    s.add_argument("--categories", default="", help="comma-separated arXiv categories (derive from the seed/surveys)")

    def common(sp):
        sp.add_argument("--review", required=True, help="review folder path or slug")

    s = sub.add_parser("search", help="run a keyword search and record term/run/links"); common(s)
    s.add_argument("--term", required=True)
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--engine", choices=["arxiv", "openalex", "s2"], default="arxiv")
    s.add_argument("--category", choices=TERM_CATEGORIES, default="concept")
    s.add_argument("--origin", default="seed", help="entity id or 'seed'/'survey:<P-id>' that motivated this term")
    s.add_argument("--rationale", default="")
    s.add_argument("--sort", choices=["relevance", "submittedDate", "lastUpdatedDate"], default="relevance")

    s = sub.add_parser("paper", help="fetch metadata for arXiv ids / P ids / DOIs and resolve OpenAlex"); common(s)
    s.add_argument("ids", nargs="+", help="P ids, arXiv ids, DOIs, or 'included'")
    s.add_argument("--no-search", action="store_true", help="skip OpenAlex title search (saves 10 credits each)")

    for name, direction in (("references", "backward"), ("citations", "forward")):
        s = sub.add_parser(name, help=f"{direction} citation walk via OpenAlex"); common(s)
        s.add_argument("pid")
        s.add_argument("--limit", type=int, default=50 if direction == "backward" else FORWARD_LIMIT)
        if direction == "backward":
            s.add_argument("--from-fulltext", action="store_true",
                           help="parse the bibliography of the cached full text instead of OpenAlex referenced_works "
                                "(automatic when OpenAlex has none)")
            s.add_argument("--no-search", action="store_true", help="bibliography walk: skip OpenAlex title searches (10 credits each)")
            s.add_argument("--all-years", action="store_true", help="bibliography walk: ignore the review's year window")
        s.set_defaults(direction=direction)
    s = sub.add_parser("snowball", help="one more hop of forward walks from included walk-found papers not yet walked; "
                       "select + screen between calls"); common(s)
    s.add_argument("--limit", type=int, default=FORWARD_LIMIT, help="citers kept per frontier paper (default %(default)s)")
    s.add_argument("--max-hop", type=int, default=10)

    s = sub.add_parser("fulltext", help="cache full text (arXiv HTML -> jina -> local pdftotext -> abstract-only)"); common(s)
    s.add_argument("pids", nargs="+", help="P ids or 'included'")
    s.add_argument("--force", action="store_true")
    s.add_argument("--published", action="store_true", help="read the open-access journal version instead of the arXiv text")

    s = sub.add_parser("enrich", help="code / checkpoint / labs / historical leaderboard rows"); common(s)
    s.add_argument("pids", nargs="+", help="P ids or 'included'")

    s = sub.add_parser("check-json", help="read-only: does a JSON file parse? (for subagents checking the files they write)")
    s.add_argument("path")
    s = sub.add_parser("voice-setup", help="one-time per machine (step 8 audio): make the Kokoro + mlx-whisper venv (~1 GB with the models)")
    s.add_argument("--no-warm", action="store_true", help="install only; the models download at the first `audio`")
    s = sub.add_parser("pwc-index", help="one-time: flatten pwc-archive evaluation-tables parquet into an index (slow, minutes)")
    s = sub.add_parser("select", help="apply caps and mark included/excluded"); common(s)
    s.add_argument("--preview", type=int, metavar="N", default=None,
                   help="write nothing; print what would be included plus the next N papers that would fill freed slots")
    s.add_argument("--share", action="append", default=[], metavar="K=N",
                   help="reserve N slots for papers screened on-topic under scope in-item K (1-based; repeat; N=0 removes); "
                        "stored in meta.caps.shares unless --preview")
    s = sub.add_parser("merge-details", help="merge data/details/*.json from step-5 subagents"); common(s)
    s.add_argument("--replace", default="", help="comma-separated P ids extracted again: their own step-5 rows are removed first")
    s = sub.add_parser("check-details", help="list quotes in data/details/<pid>.json that merge-details would reject"); common(s)
    s.add_argument("pid")
    s = sub.add_parser("status", help="every pipeline step: done / todo / stale, why, and the next command (read-only)"); common(s)
    s.add_argument("--json", action="store_true", help="print the rows as JSON")
    s = sub.add_parser("details-status", help="step 5: included papers with no details file, invalid files, rejected-quote counts"); common(s)
    s = sub.add_parser("validate", help="FK, evidence, caps, vocab checks"); common(s)
    s.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    s = sub.add_parser("render", help="markdown + csv + xlsx + REVIEW.md + hub.html"); common(s)
    s = sub.add_parser("derive", help="recompute derived link fields"); common(s)
    s = sub.add_parser("show", help="print one record"); common(s)
    s.add_argument("id")
    s = sub.add_parser("import", help="upsert records from a JSON file (list or single object)"); common(s)
    s.add_argument("--table", choices=TABLES, required=True)
    s.add_argument("--file", required=True)
    s = sub.add_parser("set", help="set one field on a record from a JSON literal"); common(s)
    s.add_argument("--table", choices=TABLES, required=True)
    s.add_argument("--id", required=True)
    s.add_argument("--field", required=True)
    s.add_argument("--json", required=True)
    s = sub.add_parser("set-meta", help="set a meta field (e.g. hub_url)"); common(s)
    s.add_argument("--field", required=True)
    s.add_argument("--json", required=True)
    s = sub.add_parser("screen", help="record a manual include/exclude decision that select will not override"); common(s)
    s.add_argument("--id", required=True, help="P id, or comma-separated P ids for one bulk decision")
    s.add_argument("--decision", choices=["included", "excluded", "candidate"], required=True)
    s.add_argument("--reason", required=True)
    s = sub.add_parser("refresh", help="alert-only: replay searched terms and citing walks for papers newer than the last run; "
                                       "adds unscreened candidates and writes rendered/REFRESH.md"); common(s)
    s.add_argument("--terms", default="", help="comma-separated term ids: replay only these (may name deferred/out-of-scope terms); skips citing walks")
    s.add_argument("--limit", type=int, default=None, help="results per replay (default: the source run's cap, min 15)")
    s.add_argument("--dry-run", action="store_true", help="print the replay plan and request count; write nothing")
    s = sub.add_parser("coverage", help="what the review may be missing: S2 neighbours of the included set and walked surveys' "
                                        "references; reruns empty reference walks and adds findings as unscreened candidates"); common(s)
    s.add_argument("--limit", type=int, default=100, help="neighbours kept (linked to >= 2 included papers, ranked by links then citations; was 40)")
    s.add_argument("--survey-limit", type=int, default=25, help="in-window references kept per walked survey, by citations")
    s.add_argument("--no-fetch", action="store_true", help="read the HTTP cache only; never touch the network")
    s.add_argument("--report-only", action="store_true", help="write data/coverage.json only: add nothing, rerun nothing")
    s = sub.add_parser("why-missing", help="is a paper in the review, and if not, why not (local: papers, links, coverage.json, runs)"); common(s)
    s.add_argument("query", help="title, arXiv id or P id")
    s = sub.add_parser("scope", help="step 0: record the scope agreement (also logged as a decision); no flags prints it"); common(s)
    s.add_argument("--in", dest="include", action="append", default=[], help="in scope (repeat)")
    s.add_argument("--out", dest="exclude", action="append", default=[], help="out of scope (repeat)")
    s.add_argument("--protocol", action="append", default=[], help="protocol rule, e.g. 'zero-shot results only' (repeat)")
    s.add_argument("--notes", default=None)
    s.add_argument("--by", choices=["user", "agent"], default="user")
    s = sub.add_parser("decide", help="log a judgement call with its reason (meta.decisions; shown on the hub's Decisions page)"); common(s)
    s.add_argument("--decision", help="what was decided")
    s.add_argument("--reason", help="why")
    s.add_argument("--kind", choices=list(DECISION_KINDS), default="other")
    s.add_argument("--refs", default="", help="comma-separated record ids (P/Q/B/M/K...)")
    s.add_argument("--by", choices=["user", "agent"], default="user")
    s.add_argument("--date", default=None, help="YYYY-MM-DD (backfilling an earlier decision)")
    s.add_argument("--list", action="store_true", help="print the decisions log")
    s.add_argument("--boundary", action="append", default=[], help="an undecided scope boundary (area) this decision settles; repeatable")
    s = sub.add_parser("delete", help="remove a record and all references to it (ids are not reused)"); common(s)
    s.add_argument("--table", choices=[t for t in TABLES if t not in LIST_TABLES], required=True)
    s.add_argument("--id", required=True)
    s = sub.add_parser("fold", help="merge a candidate dataset/benchmark/metric into an existing one (repoint rows, keep name as alias, delete)"); common(s)
    s.add_argument("--table", choices=["datasets", "benchmarks", "metrics"], required=True)
    s.add_argument("--from", dest="from_id", required=True)
    s.add_argument("--into", dest="into_id", required=True)
    s.add_argument("--variant-from-name", action="store_true", help="metrics: rows without metric_variant take the folded metric's name as variant")
    s.add_argument("--keep-alias", action="store_true",
                   help="keep the folded name as an alias even when it is a generic part of the target's name (skipped by default)")
    s = sub.add_parser("dataset-info", help="pwc-archive + Hugging Face facts about a dataset name (for step 3)"); common(s)
    s.add_argument("--name", required=True)
    s = sub.add_parser("list", help="list ids/titles/status for a table"); common(s)
    s.add_argument("--table", choices=TABLES, default="papers")
    s.add_argument("--status", default=None)

    s = sub.add_parser("kpi", help="step 6: suggest / add / remove / list headline KPIs"); common(s)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--suggest", action="store_true", help="boards by coverage, split labels grouped")
    g.add_argument("--list", action="store_true")
    g.add_argument("--add", action="store_true")
    g.add_argument("--remove", metavar="KID")
    s.add_argument("--benchmark"); s.add_argument("--dataset"); s.add_argument("--metric")
    s.add_argument("--metric-variant", default=None, help="rank only rows with this metric_variant (e.g. a threshold like '@5')")
    s.add_argument("--split", action="append", default=[], help="repeatable; 'none' = rows without a split label")
    s.add_argument("--rationale", default="")
    s = sub.add_parser("rank-papers", help="step 6: rank included papers on the KPIs (derived)"); common(s)
    s.add_argument("--top", type=int, default=None)

    s = sub.add_parser("figures", help="step 6: figure URLs + captions from cached arXiv HTML"); common(s)
    s.add_argument("pids", nargs="+", help="P ids or 'included'")

    s = sub.add_parser("cluster-input", help="step 6: JSON packet for the clustering subagent"); common(s)
    s = sub.add_parser("merge-clusters", help="step 6: import data/clusters/clusters.json"); common(s)

    s = sub.add_parser("explainer-input", help="step 6: JSON packet for one cluster's explainer subagent"); common(s)
    s.add_argument("cid")
    s = sub.add_parser("merge-explainers", help="step 6: import data/explainers/C###.json"); common(s)

    s = sub.add_parser("screen-input", help="step 1: packets of unscreened candidates for the screening subagents"); common(s)
    s.add_argument("--ids", default=None, help="comma-separated P ids (e.g. to rescreen excluded papers)")
    s.add_argument("--status", default="candidate")
    s.add_argument("--batch", type=int, default=150)
    s.add_argument("--needs-text", action="store_true", help="the candidates whose verdict was needs-text (run fulltext first)")
    s = sub.add_parser("merge-screening", help="step 1: apply data/screening/batch-*.json verdicts"); common(s)
    s.add_argument("--rescreen", action="store_true", help="also apply verdicts to papers already excluded")
    s.add_argument("--batch", default=None, help="comma-separated batch numbers to apply (default: every batch file)")
    s = sub.add_parser("adjacent-input", help="step 7: JSON packet of excluded papers for the adjacent-topics subagent"); common(s)
    s.add_argument("--abstract-chars", type=int, default=300)
    s = sub.add_parser("merge-adjacent", help="step 7: import data/adjacent/topics.json (hub tab 'Adjacent topics')"); common(s)
    s = sub.add_parser("report-input", help="step 7: JSON packet for the review-report subagent (the ten sections; hub tab 'Report')"); common(s)
    s = sub.add_parser("merge-report", help="step 7: check data/report/report.json and store it in meta.report"); common(s)
    s = sub.add_parser("synthesis-input", help="step 6: JSON packet for the review-synthesis subagent"); common(s)
    s = sub.add_parser("merge-synthesis", help="step 6: import data/explainers/synthesis.json into meta"); common(s)
    s = sub.add_parser("limitations-input", help="step 6: JSON packet for the review-limitations subagent (problem x family matrix)"); common(s)
    s = sub.add_parser("merge-limitations", help="step 6: import data/explainers/limitations.json into meta"); common(s)
    s = sub.add_parser("presentation", help="step 8 opt-in: record that the person wants the presentation (chapter plan, script, video)"); common(s)
    s.add_argument("--minutes", type=int, default=PRES_DEFAULT_MINUTES, help=f"target length in minutes ({PRES_MINUTES[0]}-{PRES_MINUTES[1]}; default {PRES_DEFAULT_MINUTES})")
    s.add_argument("--audience", help="who will watch (default: researchers in a neighbouring field)")
    s.add_argument("--off", action="store_true", help="the person does not want it")
    s.add_argument("--by", choices=["user", "agent"], default="user")
    s = sub.add_parser("presentation-input", help="step 8: JSON packet for the chapter-plan subagent"); common(s)
    s = sub.add_parser("merge-presentation-plan", help="step 8: check and store data/presentation/plan.json (exit 1 on errors)"); common(s)
    s = sub.add_parser("script-input", help="step 8: JSON packet for one chapter's script subagent"); common(s)
    s.add_argument("--chapter", required=True, help="CH01, CH02, ...")
    s = sub.add_parser("check-script", help="step 8: the merge's checks on one chapter script, storing nothing (exit 1 on errors)"); common(s)
    s.add_argument("--chapter", required=True)
    s = sub.add_parser("merge-script", help="step 8: check and store data/presentation/script/CH##.json (exit 1 when any is rejected)"); common(s)
    s.add_argument("--chapter", help="only this chapter")
    s = sub.add_parser("video", help="step 8: render the narrated MP4 from the script and the recorded audio (Chrome + ffmpeg), with captions, a sync check and the slides as a PowerPoint deck"); common(s)
    s.add_argument("--workers", type=int, default=4, help="parallel ffmpeg encodes")
    s.add_argument("--no-sync", action="store_true", help="skip the speech-recognition sync check on the finished MP4")
    s = sub.add_parser("slides", help="step 8: only the video's slides as a PowerPoint deck (one slide per scene, reveals on clicks, narration in the notes; Chrome, no ffmpeg)"); common(s)
    s = sub.add_parser("audio", help="step 8: speak the merged script (Kokoro), check it with speech recognition, store the measured times"); common(s)
    s.add_argument("--chapter", help="only this chapter")
    s.add_argument("--voice", help=f"Kokoro voice (default {VOICE_DEFAULT}; am_michael, bf_emma, ...)")
    s.add_argument("--sample", action="store_true", help="speak only 3 sentences (a term, a number, a transition) to rendered/presentation/sample.wav")
    s.add_argument("--no-asr", action="store_true", help="skip the speech-recognition check")
    s.add_argument("--force", action="store_true", help="speak every sentence again")
    s = sub.add_parser("emphasis-input", help="step 6: JSON packet of the long texts with no highlights, for the emphasis subagent"); common(s)
    s = sub.add_parser("merge-emphasis", help="step 6: write data/explainers/emphasis.json into the step-6 files and re-merge them"); common(s)
    return ap


def _dispatch(a) -> int:
    if a.cmd == "init":
        rv = Review.init(a.path, slug=a.slug, purpose=a.purpose, seed=a.seed, max_papers=a.max_papers, years=a.years,
                         seminal_citations=a.seminal_citations, categories=[c for c in a.categories.split(",") if c], max_seminal=a.max_seminal)
        print(json.dumps({"path": str(rv.path), "meta": rv.meta}, indent=1))
        return 0

    if a.cmd == "pwc-index":
        print(json.dumps(build_pwc_index()))
        return 0

    if a.cmd == "voice-setup":
        print(json.dumps(voice_setup(warm=not a.no_warm), indent=1))
        return 0

    rv = _open_review(a.review)

    def pids_arg(xs):
        return expand_ids(rv, xs)

    if a.cmd == "search":
        print(json.dumps(cmd_search(rv, a.term, a.limit, a.engine, a.category, a.origin, a.rationale, a.sort)))
    elif a.cmd == "paper":
        print(json.dumps(cmd_paper(rv, pids_arg(a.ids), no_search=a.no_search)))
    elif a.cmd in ("references", "citations"):
        print(json.dumps(cmd_graph(rv, a.pid, a.direction, a.limit, getattr(a, "from_fulltext", False),
                                   getattr(a, "no_search", False), getattr(a, "all_years", False))))
    elif a.cmd == "snowball":
        res = cmd_snowball(rv, a.limit, a.max_hop)
        print(json.dumps(res, indent=1))
        if res.get("converged"):
            return 0    # nothing walked or added: don't stamp the step log (it would make select/coverage stale)
    elif a.cmd == "fulltext":
        for pid in pids_arg(a.pids):
            print(json.dumps(cmd_fulltext(rv, pid, force=a.force, published=a.published)))
    elif a.cmd == "enrich":
        print(json.dumps(cmd_enrich(rv, pids_arg(a.pids)), indent=1))
    elif a.cmd == "select":
        shares = merge_shares(rv, a.share)
        if a.preview is not None:
            print(json.dumps(select_preview(rv, a.preview, shares=shares), indent=1, ensure_ascii=False))
            return 0
        if a.share:
            rv.meta["caps"]["shares"] = shares
        print(json.dumps(select_papers(rv)))
    elif a.cmd == "merge-details":
        rep = merge_details(rv, replace=[x for x in a.replace.split(",") if x])
        print(json.dumps(rep, indent=1))
        if rep["missing_details"]:
            print(f"WARNING: {len(rep['missing_details'])} included paper(s) have no data/details file: "
                  f"{', '.join(rep['missing_details'])}. Relaunch their step-5 agents and merge again.", file=sys.stderr)
    elif a.cmd == "check-details":
        bad = check_details(rv, a.pid)
        for b in bad:
            print(f"NOT IN TEXT  {b['field']}: {b['quote']!r}")
        notes = details_row_notes(json.loads((rv.path / "data" / "details" / f"{a.pid}.json").read_text()))
        for n in notes:
            print(f"ROW  {n}")
        print(f"{len(bad)} quote(s) would be rejected by merge-details; {len(notes)} row note(s)")
        sys.exit(1 if bad or notes else 0)
    elif a.cmd == "status":
        st = review_status(rv)
        if a.json:
            print(json.dumps(st, indent=1, ensure_ascii=False))
            return 0
        for r in st["rows"]:
            print(f"{r['step']}  {r['state'].upper():8} {r['key']:14} {r['detail']}" + (f"\n{'':27}-> {r['next']}" if r["next"] else ""))
        n = st["next"]
        print(f"\nnext: step {n['step']} {n['key']}: {n['next'] or n['detail']}" if n else "\nnothing todo or stale")
        return 0
    elif a.cmd == "details-status":
        st = details_status(rv)
        for pid in st["missing"]:
            print(f"MISSING  {pid}: no data/details/{pid}.json")
        for pid in st["invalid"]:
            print(f"INVALID  {pid}: data/details/{pid}.json is not valid JSON")
        for pid, n in st["rejected_quotes"].items():
            print(f"QUOTES   {pid}: {n} quote(s) merge-details would reject (check-details {pid})")
        for pid in st["not_included"]:
            print(f"IGNORED  {pid}: details file for a paper that is not an included non-survey paper (merge-details skips it)")
        print(f"{len(st['ok'])} ok, {len(st['missing'])} missing, {len(st['invalid'])} invalid, "
              f"{len(st['rejected_quotes'])} with rejected quotes")
        return 1 if st["missing"] or st["invalid"] else 0
    elif a.cmd == "validate":
        errors, warnings = validate_review(rv)
        for e in errors:
            print("ERROR:", e)
        for w in warnings:
            print("WARN: ", w)
        print(f"{len(errors)} errors, {len(warnings)} warnings")
        return 1 if errors or (a.strict and warnings) else 0
    elif a.cmd == "render":
        res = render_review(rv)
        print(json.dumps(res, indent=1))
        if res.get("hub_over_limit"):      # not logged: `status` keeps step 7 open
            raise SystemExit(res["hub_over_limit"])
    elif a.cmd == "derive":
        rv.derive()
        print("ok")
    elif a.cmd == "show":
        for t in TABLES:
            if t not in LIST_TABLES and a.id in rv.tables[t]:
                print(json.dumps(rv.tables[t][a.id], indent=1, ensure_ascii=False))
                break
        else:
            raise SystemExit(f"{a.id} not found")
        return 0
    elif a.cmd == "import":
        recs = json.loads(Path(a.file).read_text())
        rep = []
        print(json.dumps(cmd_import(rv, a.table, recs if isinstance(recs, list) else [recs], report=rep)))
        for line in rep:
            print(line, file=sys.stderr)
    elif a.cmd == "set":
        tbl = rv.tables[a.table]
        if a.id not in tbl:
            raise SystemExit(f"{a.id} not in {a.table}")
        tbl[a.id][a.field] = json.loads(a.json)
        rv.derive()
        print("ok")
    elif a.cmd == "set-meta":
        rv.meta[a.field] = json.loads(a.json)
        print("ok")
    elif a.cmd == "fold":
        print(json.dumps(fold(rv, a.table, a.from_id, a.into_id, a.variant_from_name, a.keep_alias)))
    elif a.cmd == "screen":
        print(json.dumps(screen(rv, a.id, a.decision, a.reason)))
    elif a.cmd == "refresh":
        res = cmd_refresh(rv, [t for t in a.terms.split(",") if t] or None, a.limit, a.dry_run)
        if a.dry_run:
            print(json.dumps(res, indent=1))
            return 0
        print(f"refresh: {len(res['replays'])} replays ({len(res['failed'])} failed, {len(res['skipped'])} skipped), "
              f"{len(res['new'])} new candidates -> {res['report']}")
    elif a.cmd == "delete":
        print(json.dumps(delete_record(rv, a.table, a.id)))
    elif a.cmd == "coverage":
        global CACHE_ONLY
        CACHE_ONLY = a.no_fetch
        cov = coverage_review(rv, a.limit, a.survey_limit)
        print(f"coverage: {cov['neighbours_total']} neighbours linked to >= {cov['min_links']} of {cov['included_count']} included "
              f"papers (kept {len(cov['neighbours'])}); {len(cov['survey_refs'])} walked surveys; {len(cov['failed'])} failed edge lists")
        g = cov["gaps"]
        print(f"gaps: {len(g['zero_ref_walks'])} empty reference walks, {len(g['skipped_terms'])} skipped terms")
        for t in g["skipped_terms"]:
            print(f"  term {t['id']} {t['term']!r} {t['status']}: {t.get('reason')}  (search it, or record why not with `decide`)")
        if a.report_only:
            return 0
        rep = coverage_apply(rv, cov)
        print(f"reran {len(rep['reran'])} empty reference walks; added {len(rep['added'])} new candidates "
              f"({rep['linked']} discovery links). Next: `select`, read the new titles, `screen`.")
        if rep["rerun_failed"]:
            print(f"rerun failed: {rep['rerun_failed']}", file=sys.stderr)
    elif a.cmd == "why-missing":
        w = why_missing(rv, a.query)
        print(json.dumps(w, indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "scope":
        if not (a.include or a.exclude or a.protocol or a.notes):
            print(json.dumps(rv.meta.get("scope"), indent=1, ensure_ascii=False))
            return 0
        print(json.dumps(set_scope(rv, a.include, a.exclude, a.protocol, a.notes, a.by), indent=1, ensure_ascii=False))
    elif a.cmd == "decide":
        if a.list:
            for d in rv.meta.get("decisions") or []:
                print(f"{d['id']}\t{d['date']}\t{d['kind']}\t{d['decision']} -- {d['reason']}")
            return 0
        d = add_decision(rv, a.decision or "", a.reason or "", a.kind, a.refs.split(","), a.by, a.date)
        for area in a.boundary:
            decide_boundary(rv, area, d["id"])
        print(json.dumps(d, ensure_ascii=False))
    elif a.cmd == "dataset-info":
        print(json.dumps(dataset_info(rv, a.name), indent=1, ensure_ascii=False, default=str))
        return 0
    elif a.cmd == "kpi":
        if a.suggest:
            print(json.dumps(kpi_suggest(rv), indent=1, ensure_ascii=False))
            return 0
        if a.list:
            print(json.dumps(rv.meta.get("kpis") or [], indent=1, ensure_ascii=False))
            return 0
        if a.add:
            splits = [None if s.strip().lower() == "none" else s for s in a.split]
            print(json.dumps(kpi_add(rv, a.benchmark, a.dataset, a.metric, splits, a.rationale, a.metric_variant), ensure_ascii=False))
        else:
            kpi_remove(rv, a.remove)
            print("ok")
    elif a.cmd == "rank-papers":
        if a.top is not None:
            rv.meta["top_n"] = a.top
        out = rank_papers(rv)
        if len(out["ranked"]) < out["top_n"]:
            print(f"warning: only {len(out['ranked'])} rankable papers for top_n {out['top_n']}", file=sys.stderr)
        print(json.dumps(out, indent=1, ensure_ascii=False))
        if a.top is None:
            return 0
    elif a.cmd == "figures":
        for pid in pids_arg(a.pids):
            print(json.dumps(cmd_figures(rv, pid)))
    elif a.cmd == "cluster-input":
        print(json.dumps(cluster_input(rv), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-clusters":
        print(json.dumps(merge_clusters(rv), indent=1, ensure_ascii=False))
    elif a.cmd == "explainer-input":
        print(json.dumps(explainer_input(rv, a.cid), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-explainers":
        print(json.dumps(merge_explainers(rv), indent=1, ensure_ascii=False))
    elif a.cmd == "screen-input":
        print(json.dumps(screen_input(rv, a.ids.split(",") if a.ids else None, a.status, a.batch, a.needs_text), indent=1))
        return 0    # read-only (no lock): writes packet files only, never the tables
    elif a.cmd == "merge-screening":
        print(json.dumps(merge_screening(rv, a.rescreen, [int(x) for x in a.batch.split(",")] if a.batch else None),
                         indent=1, ensure_ascii=False))
    elif a.cmd == "adjacent-input":
        print(json.dumps(adjacent_input(rv, a.abstract_chars), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-adjacent":
        print(json.dumps(merge_adjacent(rv), indent=1, ensure_ascii=False))
    elif a.cmd == "report-input":
        print(json.dumps(report_input(rv), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-report":
        rep = merge_report(rv)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        if rep["errors"]:
            return 1
    elif a.cmd == "synthesis-input":
        print(json.dumps(synthesis_input(rv), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-synthesis":
        print(json.dumps(merge_synthesis(rv), indent=1, ensure_ascii=False))
    elif a.cmd == "limitations-input":
        print(json.dumps(limitations_input(rv), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "presentation":
        print(json.dumps(presentation_set(rv, minutes=a.minutes, audience=a.audience, by=a.by, off=a.off), indent=1, ensure_ascii=False))
    elif a.cmd == "presentation-input":
        print(json.dumps(presentation_input(rv), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-presentation-plan":
        rep = merge_presentation_plan(rv)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        if rep["errors"]:
            return 1
    elif a.cmd == "script-input":
        print(json.dumps(script_input(rv, a.chapter), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "check-script":
        rep = check_script(rv, a.chapter)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        return 0 if rep["ok"] else 1
    elif a.cmd == "merge-script":
        rep = merge_script(rv, a.chapter)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        if not rep["merged"]:
            return 1
        if rep["rejected"]:
            rv.meta.setdefault("step_log", {})["merge-script"] = now_iso()
            rv.save()
            return 1
    elif a.cmd == "video":
        rep = video_build(rv, workers=a.workers, sync=not a.no_sync)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
    elif a.cmd == "slides":
        print(json.dumps(slides_build(rv), indent=1, ensure_ascii=False))
        return 0             # writes only the deck: no step-log entry, or `status` would call render and publish stale
    elif a.cmd == "audio":
        rep = audio_build(rv, chapter=a.chapter, voice=a.voice, sample=a.sample, asr=not a.no_asr, force=a.force)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        if a.sample:
            return 0
    elif a.cmd == "merge-limitations":
        print(json.dumps(merge_limitations(rv), indent=1, ensure_ascii=False))
    elif a.cmd == "emphasis-input":
        print(json.dumps(emphasis_input(rv), indent=1, ensure_ascii=False))
        return 0
    elif a.cmd == "merge-emphasis":
        print(json.dumps(merge_emphasis(rv), indent=1, ensure_ascii=False))
    elif a.cmd == "list":
        data = rv.tables[a.table]
        recs = data if isinstance(data, list) else data.values()
        for r in recs:
            if a.status and r.get("status") != a.status:
                continue
            label = r.get("title") or r.get("name") or r.get("term") or r.get("query") or ""
            print(f"{r.get('id', '')}\t{r.get('status', '')}\t{r.get('year', '')}\t{label[:100]}")
        return 0
    log = rv.meta.setdefault("step_log", {})                    # `status` reads it to find stale steps
    log[f"set-meta:{a.field}" if a.cmd == "set-meta" else a.cmd] = now_iso()
    if a.cmd == "merge-details":   # content hashes, not mtimes: a git checkout touches every file
        rv.meta["details_merged"] = details_hashes(rv)
    rv.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
