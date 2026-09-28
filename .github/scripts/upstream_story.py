#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# upstream_story.py  ·  Regenerates the "Open-source work" section of README.md
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: the daily job behind this script used to keep exactly ONE
# number fresh - "25 PRs opened in total (1 open, 24 closed)" on a single third-
# party repository, none of them merged. That is the weakest true statement
# available about this profile, and it was the only one being refreshed. The
# refresh machinery was fine; it pointed at the wrong target.
#
# It now renders, from live public data only:
#   1. ADOPTION - npm downloads over a rolling year (the one metric that does
#      not collapse in a quiet month), plus the rolling month as a footnote.
#      The package list is DISCOVERED at run time from the npm registry search
#      (maintainer:m_guttmann), so a newly published package enters the sum
#      without a code change. A package is named and linked only when its
#      GitHub repository is provably public at render time (it appears in an
#      `is:public` repository search); every other package still counts
#      towards the total, unnamed.
#   2. BREADTH - the number of upstream repositories the author does not own
#      and has contributed to (cumulative, so it cannot shrink overnight).
#   3. MERGED UPSTREAM - deliberately DATE-FREE: the merger's identity
#      (derived from `mergedBy`), the repository's star magnitude floored to
#      thousands, the count, the three NEWEST merged titles and a link to the
#      full list. Only a merge by someone other than the author counts. The
#      block has a bounded length: at most three titles plus the link per
#      repository and at most MAX_MERGED_REPOS (5) repositories. A repository
#      with three or more counted merges only raises its count on the next
#      merge; one with fewer gains a title line, and a newly merged-into
#      repository adds its block until the cap of five is reached. A merge
#      does not age, so no date is printed.
#   4. OPEN RIGHT NOW - at most three items, each with an ISO date. A DATE,
#      never a distance: "3 days ago" turns into "89 days ago" without anyone
#      touching it, and this account's public activity swings by a factor of 62
#      between months. No streaks, no "last active", no 30-day counters.
#   5. THE CLOSED-PR TRUTH - who actually closed the closed pull requests,
#      counted live from CLOSED_EVENT actors instead of asserted in prose.
#
# Two marker pairs are rewritten: STORY (the data) and FRESH (the run stamp).
# The stamp is Markdown TEXT, never an image: every README image is proxied
# through camo.githubusercontent.com, which caches aggressively, so a rendered
# timestamp is exactly the kind of "freshness proof" that goes stale silently.
#
# TOKEN HANDLING (security-critical):
#   - The token is read from GH_TOKEN, then GITHUB_TOKEN. Nothing else.
#   - It is sent as an Authorization header to GITHUB_GRAPHQL and to NOTHING
#     else: api.npmjs.org and registry.npmjs.org are public and are queried
#     with no credentials at all.
#   - It is never logged, never interpolated into an error message, and never
#     written to the README.
#
# PRIVACY - FOUR GUARDS: the search query below, run with a privileged
# organisation token instead of the workflow's own GITHUB_TOKEN, returns
# hundreds of PRIVATE items complete with vendor and audit titles. Every guard
# exists to make that outcome impossible, and every guard has its own unit test:
#   (a) TOKEN SOURCE - only GH_TOKEN/GITHUB_TOKEN is ever read. The repository
#       also holds a privileged, organisation-wide stats secret (used by
#       activity-composite.yml); its name deliberately does not appear anywhere
#       in this file, so no copy-paste edit can pick it up, and a unit test
#       asserts that the name stays absent.
#   (b) QUERY - every search query carries `is:public`, asserted at runtime by
#       assert_query_is_public() BEFORE the first request goes out.
#   (c) RESULT SET - assert_public() aborts the whole run if any returned item
#       has repository.isPrivate != false, INCLUDING the case where the field is
#       missing. Fail-closed: absence of proof is treated as private. The abort
#       message deliberately does not contain the repository name.
#   (d) OWNER DENYLIST - OWNER_DENYLIST is checked twice: on the raw API nodes
#       and again on the fully rendered block, so a future rendering path cannot
#       route a denylisted owner into the README behind the first check.
#
# SSRF: GITHUB_GRAPHQL, NPM_API and NPM_SEARCH are HARDCODED module constants. None is
# derived from the environment, from an argument or from a response, so no input
# can redirect a request (the unit tests monkeypatch the attributes in-process,
# exactly like the tests for graphql_split_proxy.py).
#
# FAIL-CLOSED: both blocks are assembled completely in memory and there is
# exactly ONE file write, at the very end. A partially rendered section is not
# "avoided", it is unreachable - a unit test counts the write calls. Two error
# classes:
#   - Fatal    (our defect: no token, broken markers, guard violation, HTTP 4xx
#              from GitHub or a non-404 4xx from a download counter, an em or
#              en dash in the rendered output)
#              -> ::error::, NOTHING written, exit 1, red job.
#   - Degraded (the outside world: network, a connection that dies mid-
#              response, 429/5xx after retries, timeout, malformed payload,
#              inconsistent snapshot, an incomplete or failed npm registry
#              search, a discovered package without download data) -> ::error::, the STORY
#              block stays byte-identical, only FRESH is rewritten and says
#              "degraded", exit 0. The degradation is visible on the public
#              README itself, not merely in a log nobody opens.
#
# DETERMINISM: every list is sorted locally (API order is never trusted), the
# timestamp and the run metadata are PARAMETERS rather than global state, and
# nothing derived from "now" other than that stamp reaches the output. Same
# input and same stamp, byte-identical output. The FRESH stamp carries the
# minute of the run, so every run that is not fatal (a degraded one included)
# rewrites FRESH and commits. That is intended: the daily commit keeps the
# scheduled workflows of this repository active against GitHub's 60-day
# inactivity disablement.
#
# Standard library only (consistency with lang_card.py / graphql_split_proxy.py).
#
# Usage:
#   python3 .github/scripts/upstream_story.py [--readme README.md] [--now ISO8601Z]
#
#   Local runs need a token and, on macOS, a CA bundle. Without one every
#   HTTPS request fails and the run degrades (exit 0, STORY unchanged).
#   The system bundle works without extra packages:
#     SSL_CERT_FILE=/etc/ssl/cert.pem \
#     GH_TOKEN=$(gh auth token) python3 .github/scripts/upstream_story.py
#   (With certifi installed, SSL_CERT_FILE=$(python3 -m certifi) works too.)
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GITHUB_GRAPHQL = "https://api.github.com/graphql"   # HARDCODED - never derived from env/args (no SSRF); tests monkeypatch this attribute
NPM_API = "https://api.npmjs.org/downloads/point"   # HARDCODED, UNAUTHENTICATED: no Authorization header ever goes here
NPM_SEARCH = "https://registry.npmjs.org/-/v1/search"  # HARDCODED, UNAUTHENTICATED, same rule
NPM_MAINTAINER = "m_guttmann"                       # the npm account, which is NOT the GitHub login
NPM_SEARCH_SIZE = 250                               # the registry's maximum page size
GITHUB_SOURCE = "the GitHub API"                    # how the two sources are named in the README stamp
NPM_SOURCE = "api.npmjs.org"
REGISTRY_SOURCE = "the npm registry search"
USER_AGENT = "mguttmann-profile-readme/upstream_story.py"

AUTHOR = "mguttmann"
OWNER_DENYLIST = ("journaway",)                     # GUARD (d) - lowercase
ATTRIBUTION_REPO = "anomalyco/opencode"
AUTOMATION_LOGIN = "github-actions"                 # the only bot login the sentence below names
SEARCH_ALL = "author:mguttmann is:public -user:mguttmann sort:updated-desc"                        # GUARD (b)
SEARCH_CLOSED = "author:mguttmann is:public is:pr is:closed is:unmerged repo:anomalyco/opencode"   # GUARD (b)
SEARCH_REPOS = "user:mguttmann is:public fork:true"                                                # GUARD (b)

STORY_START, STORY_END = "<!-- STORY:START -->", "<!-- STORY:END -->"
FRESH_START, FRESH_END = "<!-- FRESH:START -->", "<!-- FRESH:END -->"
LANG_NOTE = ("<!-- LANG: prose below is GENERATED, translate for a DE variant; "
             "every number is fetched at run time. -->")
NPM_DISCIPLINE = ("<sub>Downloads, not users: CI and mirror traffic is included. "
                  "Source: api.npmjs.org.</sub>")
SCHEDULE_NOTE = "scheduled daily at 04:30 UTC"      # "scheduled", not "rebuilt": scheduled runs actually start ~05:20-05:45 UTC

# Rendered only when a label VALIDATES the report (someone else confirmed it).
LABEL_ALLOWLIST = ("bug", "has repro", "regression", "confirmed", "reproduced",
                   "security", "help wanted", "good first issue")
# An item carrying one of these is not shown at all - plastering a dismissed
# item as "open work" is worse than showing nothing.
LABEL_DENYLIST = ("duplicate", "invalid", "spam", "wontfix")

# npm's own name rule (lowercase, optional scope). A discovered name that breaks
# it is a malformed payload: it would otherwise reach a URL path and the README.
NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
# The package's declared repository, e.g. git+https://github.com/mguttmann/x.git
GITHUB_REPO_URL = re.compile(
    r"^(?:git\+)?(?:https?|git|ssh|git\+ssh)://(?:git@)?github\.com/"
    r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")

# Every em dash, en dash and their look-alikes. Built from code points so this
# file itself stays free of them; foreign titles are normalised to "-".
DASHES = "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015, 0x2E3A, 0x2E3B,
                                  0xFE31, 0xFE32, 0xFE58, 0xFE63))
FORBIDDEN_DASHES = (chr(0x2013), chr(0x2014))       # the owner's text rule


def _entity_pattern(code_points: tuple[int, ...], names: tuple[str, ...]) -> re.Pattern:
    """HTML character references that GitHub renders as one of the given
    code points: the named forms plus decimal and hexadecimal numeric forms
    (any case, leading zeros allowed), each with its semicolon. A doubly
    escaped `&amp;mdash;` is NOT matched: it renders as the literal text."""
    alternatives = ["&%s;" % name for name in names]
    for cp in code_points:
        alternatives.append("&#0*%d;" % cp)
        alternatives.append("&#[xX]0*%s;" % "".join(
            "[%s%s]" % (d.lower(), d.upper()) if d.isalpha() else d for d in "%x" % cp))
    return re.compile("|".join(alternatives))


# Entity forms of every code point in DASHES (input side) and of the two
# forbidden ones (output side).
DASH_ENTITIES = _entity_pattern(tuple(ord(d) for d in DASHES), ("mdash", "ndash", "horbar"))
FORBIDDEN_DASH_ENTITIES = _entity_pattern((0x2013, 0x2014), ("mdash", "ndash"))

MAX_NEWEST_MERGED = 3                               # per repository; the rest is a count plus a link
MAX_OPEN_ITEMS, MAX_MERGED_REPOS, MAX_TITLE = 3, 5, 96
MAX_PAGES = 10                                      # 100 items per page; a cursor that never advances cannot loop forever
HTTP_TIMEOUT = 30
RETRY_BACKOFF = (1.0, 4.0)                          # 3 attempts total
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
RETRYABLE_GRAPHQL_TYPES = frozenset({"RESOURCE_LIMITS_EXCEEDED"})

# Verified live on 2026-07-25: rateLimit.cost == 1 per page. `labels(first: 20)`
# is load-bearing - with first: 5 the validating labels of an item that carries
# eight of them (routing labels first) fall off the end and silently disappear.
MAIN_QUERY = """
query($q: String!, $after: String) {
  rateLimit { cost remaining }
  search(type: ISSUE, query: $q, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on PullRequest {
        number title url state updatedAt mergedAt
        mergedBy { login }
        labels(first: 20) { nodes { name } }
        repository { nameWithOwner isFork isPrivate stargazerCount owner { login } }
      }
      ... on Issue {
        number title url state updatedAt
        labels(first: 20) { nodes { name } }
        repository { nameWithOwner isFork isPrivate stargazerCount owner { login } }
      }
    }
  }
}
"""

# Who closed the closed pull requests? Asking beats claiming: the moment a
# maintainer closes one, the rendered sentence changes itself instead of
# quietly becoming false.
ATTRIBUTION_QUERY = """
query($q: String!, $after: String) {
  rateLimit { cost remaining }
  search(type: ISSUE, query: $q, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        repository { nameWithOwner isFork isPrivate owner { login } }
        timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
          nodes { ... on ClosedEvent { actor { login __typename } } }
        }
      }
    }
  }
}
"""

# Counters for the closing status line only - no decision reads them.
_STATS = {"cost": 0, "pages": 0, "npm_ok": 0}


class Degraded(Exception):
    """Transient/external failure -> degraded FRESH note, STORY untouched, exit 0."""


class Fatal(Exception):
    """Our defect or a guard violation -> nothing written at all, exit 1."""


class _Retry(Exception):
    """Internal: this attempt failed in a way the retry ALLOWLIST covers."""


def log(msg: str) -> None:
    """Status line for the workflow log. NEVER pass a token or a raw payload."""
    sys.stdout.write("upstream_story: %s\n" % msg)
    sys.stdout.flush()


def error(msg: str) -> None:
    """Workflow error annotation - visible on the run page, not only in the log."""
    sys.stdout.write("::error::upstream_story: %s\n" % msg)
    sys.stdout.flush()


# ── token ────────────────────────────────────────────────────────────────────

def token() -> str:
    """GUARD (a): the ONLY two environment variables this script ever reads for
    credentials. Both carry the workflow's built-in, repository-scoped token,
    which cannot see private organisation repositories."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    raise Fatal("no GH_TOKEN/GITHUB_TOKEN in the environment - refusing to run")


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _wants_retry(parsed: dict) -> bool:
    """GraphQL cost-guard errors arrive inside an HTTP 200 and are load-
    dependent, not a property of the query - the only payload worth retrying.
    npm's point responses have no `errors` key, so this is a no-op for them."""
    errors = parsed.get("errors")
    if not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and e.get("type") in RETRYABLE_GRAPHQL_TYPES
               for e in errors)


def _fetch_once(url: str, *, data: bytes | None, headers: dict[str, str], source: str) -> dict:
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in RETRYABLE_STATUS:
            raise _Retry("HTTP %d" % exc.code) from None
        if 400 <= exc.code < 500:
            # A 4xx that is not a rate limit is OUR defect - a missing or
            # expired token, a query the schema rejects, a package name that no
            # longer exists. Retrying cannot fix it; retrying would hide it.
            raise Fatal("HTTP %d from %s" % (exc.code, source)) from None
        raise Degraded("%s was unavailable" % source) from None
    except (OSError, http.client.HTTPException) as exc:
        # The whole TRANSPORT class, not a list of instances: urllib wraps only
        # errors raised while SENDING into URLError. A connection that dies
        # while the response is being read surfaces as RemoteDisconnected,
        # ConnectionResetError or IncompleteRead, and each of these is either
        # an OSError (URLError, TimeoutError, ssl errors, every socket error)
        # or an http.client.HTTPException. HTTPError is handled above.
        # Class name only: an exception message can echo the URL back.
        raise _Retry(exc.__class__.__name__) from None
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        log("%s: malformed response body" % source)
        raise Degraded("%s was unavailable" % source)
    if _wants_retry(parsed):
        raise _Retry("resource limits")
    return parsed


def _fetch(url: str, *, data: bytes | None, headers: dict[str, str], source: str) -> dict:
    """One request plus the retry ALLOWLIST of D11 - never a denylist, so a
    genuine error can never hide behind three silent attempts."""
    attempts = len(RETRY_BACKOFF) + 1
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_once(url, data=data, headers=headers, source=source)
        except _Retry as exc:
            if attempt == attempts:
                break
            delay = RETRY_BACKOFF[attempt - 1]
            log("%s: transient failure (%s) - retry %d/%d in %.0fs"
                % (source, exc, attempt, attempts - 1, delay))
            time.sleep(delay)
    raise Degraded("%s was unavailable" % source)


def graphql(query: str, variables: dict[str, object], tok: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        # Sent to the hardcoded GITHUB_GRAPHQL and to nothing else; never logged.
        "Authorization": "Bearer %s" % tok,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    parsed = _fetch(GITHUB_GRAPHQL, data=body, headers=headers, source=GITHUB_SOURCE)
    errors = parsed.get("errors")
    if errors:
        # Retryable types already spent their attempts inside _fetch. Types are
        # logged, not rendered: the README says only that the source failed.
        types = sorted({str(e.get("type")) for e in errors if isinstance(e, dict)})
        log("%s: GraphQL errors %s" % (GITHUB_SOURCE, ",".join(types) or "unspecified"))
        raise Degraded("%s was unavailable" % GITHUB_SOURCE)
    data = parsed.get("data")
    if not isinstance(data, dict):
        log("%s: response without a data object" % GITHUB_SOURCE)
        raise Degraded("%s was unavailable" % GITHUB_SOURCE)
    rate = data.get("rateLimit")
    if isinstance(rate, dict) and isinstance(rate.get("cost"), int):
        _STATS["cost"] += rate["cost"]
    return data


def search_all(query_doc: str, q: str, tok: str) -> list[dict]:
    assert_query_is_public(q)          # GUARD (b) - before the first request
    nodes: list[dict] = []
    after: str | None = None
    for _page in range(MAX_PAGES):
        data = graphql(query_doc, {"q": q, "after": after}, tok)
        search = data.get("search")
        if not isinstance(search, dict) or not isinstance(search.get("nodes"), list):
            log("%s: unexpected search payload" % GITHUB_SOURCE)
            raise Degraded("%s was unavailable" % GITHUB_SOURCE)
        _STATS["pages"] += 1
        # Empty nodes appear when a result does not match any inline fragment;
        # they carry no data, so dropping them cannot hide anything.
        nodes.extend(n for n in search["nodes"] if isinstance(n, dict) and n)
        info = search.get("pageInfo")
        info = info if isinstance(info, dict) else {}
        if not info.get("hasNextPage"):
            return nodes
        after = info.get("endCursor")
    log("%s: pagination did not terminate within %d pages" % (GITHUB_SOURCE, MAX_PAGES))
    raise Degraded("%s was unavailable" % GITHUB_SOURCE)


def fetch_items(tok: str) -> list[dict]:
    return search_all(MAIN_QUERY, SEARCH_ALL, tok)


def fetch_close_attribution(tok: str) -> dict[str, int]:
    """Bucket the last CLOSED_EVENT actor of every closed, unmerged PR.

    `actor.__typename == "Bot"` is the robust bot test - the login string is
    not. Anything that is neither the named automation nor the author lands in
    "other", INCLUDING actor == null: an unknown closer is counted in the
    direction that is unfavourable to the author, never the flattering one."""
    nodes = search_all(ATTRIBUTION_QUERY, SEARCH_CLOSED, tok)
    assert_public(nodes)               # GUARDS (c)+(d) on this query's raw nodes too
    buckets = {"bot": 0, "self": 0, "other": 0, "closed": 0}
    for node in nodes:
        buckets["closed"] += 1
        events = (node.get("timelineItems") or {}).get("nodes")
        last = events[-1] if isinstance(events, list) and events else None
        actor = last.get("actor") if isinstance(last, dict) else None
        if not isinstance(actor, dict):
            buckets["other"] += 1
        elif actor.get("__typename") == "Bot" and actor.get("login") == AUTOMATION_LOGIN:
            buckets["bot"] += 1
        elif actor.get("login") == AUTHOR:
            buckets["self"] += 1
        else:
            buckets["other"] += 1
    return buckets


REPOS_QUERY = """
query($q: String!, $after: String) {
  rateLimit { cost remaining }
  search(type: REPOSITORY, query: $q, first: 100, after: $after) {
    repositoryCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on Repository { name isPrivate owner { login } } }
  }
}
"""


def fetch_public_repos(tok: str) -> set[str]:
    """The author's repositories that are PROVABLY public right now, lowercased.

    Proof means: returned by an `is:public` search (GUARD (b)) AND carrying
    isPrivate == false. Anything else, including a missing field, is a guard
    violation, exactly as for the item search. A repository absent from this
    set is simply not named; absence can never make a package appear."""
    nodes = search_all(REPOS_QUERY, SEARCH_REPOS, tok)
    public: set[str] = set()
    for node in nodes:
        if node.get("isPrivate") is not False:
            raise Fatal("private repository in a public-only result set - aborting")
        owner = (node.get("owner") or {}).get("login") or ""
        if owner.lower() in OWNER_DENYLIST:
            raise Fatal("denylisted owner in a public-only result set - aborting")
        if owner.lower() == AUTHOR and isinstance(node.get("name"), str):
            public.add(node["name"].lower())
    return public


def discover_packages() -> list[dict]:
    """Every npm package the maintainer publishes, from the registry search.

    The sum must never be presented as complete when it is not, so the result
    is checked for completeness: `total` must be an integer equal to the number
    of returned objects (a result set larger than one page, or a truncated one,
    degrades). Only packages that list NPM_MAINTAINER as a maintainer count;
    the search is a text search and must not smuggle in foreign packages.

    Each package carries its declared GitHub repository name when that
    repository lives under the author's account, else None."""
    url = "%s?%s" % (NPM_SEARCH, urllib.parse.urlencode(
        {"text": "maintainer:%s" % NPM_MAINTAINER, "size": NPM_SEARCH_SIZE}))
    try:
        # No Authorization header: this endpoint is public and must never see a token.
        parsed = _fetch(url, data=None,
                        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                        source=REGISTRY_SOURCE)
    except Fatal:
        # The brief is explicit: a failed registry search degrades, whatever
        # the status code. It can never yield a partial package list.
        log("%s: rejected the request" % REGISTRY_SOURCE)
        raise Degraded("%s was unavailable" % REGISTRY_SOURCE) from None
    objects, total = parsed.get("objects"), parsed.get("total")
    if not isinstance(objects, list) or not isinstance(total, int) or total != len(objects):
        # A foreign value is never logged raw: a non-integer total is reported
        # by its type name only.
        shown = str(total) if isinstance(total, int) else "total of type %s" % type(total).__name__
        log("%s: incomplete result (%s of %s)"
            % (REGISTRY_SOURCE, len(objects) if isinstance(objects, list) else "?", shown))
        raise Degraded("%s returned an incomplete package list" % REGISTRY_SOURCE)
    packages: dict[str, dict] = {}
    for obj in objects:
        package = obj.get("package") if isinstance(obj, dict) else None
        if not isinstance(package, dict):
            log("%s: malformed package entry" % REGISTRY_SOURCE)
            raise Degraded("%s was unavailable" % REGISTRY_SOURCE)
        name = package.get("name")
        if not isinstance(name, str) or not NPM_NAME.match(name):
            log("%s: malformed package name" % REGISTRY_SOURCE)
            raise Degraded("%s was unavailable" % REGISTRY_SOURCE)
        maintainers = package.get("maintainers")
        if not any(isinstance(m, dict) and m.get("username") == NPM_MAINTAINER
                   for m in maintainers or []):
            continue
        repo = None
        links = package.get("links")
        link = links.get("repository") if isinstance(links, dict) else None
        match = GITHUB_REPO_URL.match(link) if isinstance(link, str) else None
        if match and match.group(1).lower() == AUTHOR:
            repo = match.group(2)
        packages[name] = {"name": name, "repo": repo}
    if not packages:
        log("%s: no package lists %s as a maintainer" % (REGISTRY_SOURCE, NPM_MAINTAINER))
        raise Degraded("%s returned an incomplete package list" % REGISTRY_SOURCE)
    return [packages[name] for name in sorted(packages)]


def fetch_npm_point(name: str, window: str) -> int:
    url = "%s/%s/%s" % (NPM_API, window, urllib.parse.quote(name, safe="@/"))
    try:
        # No Authorization header: this endpoint is public and must never see a token.
        parsed = _fetch(url, data=None,
                        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                        source=NPM_SOURCE)
    except Fatal as exc:
        if str(exc) != "HTTP 404 from %s" % NPM_SOURCE:
            raise
        # The registry lists the package but the counter does not know it: the
        # two sources disagree, and a sum without it would be incomplete.
        log("%s: no download data for %s" % (NPM_SOURCE, name))
        raise Degraded("%s returned an inconsistent snapshot" % NPM_SOURCE) from None
    downloads = parsed.get("downloads")
    if not isinstance(downloads, int):
        log("%s: %s/%s carries no integer download count" % (NPM_SOURCE, window, name))
        raise Degraded("%s was unavailable" % NPM_SOURCE)
    return downloads


def collect_npm(packages: list[dict], public_repos: set[str]) -> dict:
    named: list[dict] = []
    month = year = unnamed_count = unnamed_year = 0
    for package in packages:
        name, repo = package["name"], package["repo"]
        # Only point/last-month and point/last-year: one request each, no range
        # or pagination mechanics, and the yearly figure cannot be argued down
        # by a quiet month.
        this_month = fetch_npm_point(name, "last-month")
        this_year = fetch_npm_point(name, "last-year")
        month += this_month
        year += this_year
        _STATS["npm_ok"] += 1
        # Named only with a provably public repository; the downloads of every
        # other package are real and still count, but a named claim needs a
        # reachable target.
        if repo and repo.lower() in public_repos:
            named.append({"name": name, "repo": repo, "month": this_month, "year": this_year})
        else:
            unnamed_count += 1
            unnamed_year += this_year
    named.sort(key=lambda p: p["name"])
    named.sort(key=lambda p: p["year"], reverse=True)
    return {"month": month, "year": year, "named": named, "count": len(packages),
            "unnamed_count": unnamed_count, "unnamed_year": unnamed_year}


# ── privacy guards ───────────────────────────────────────────────────────────

def assert_public(nodes: list[dict]) -> None:
    """GUARD (c) + GUARD (d) on the RAW API nodes.

    A missing isPrivate field counts as private (fail-closed): the guard
    demands proof of publicness, not absence of proof of privacy. The abort
    message must never name the repository - that name is exactly the string
    this guard exists to keep out of a public log."""
    for node in nodes:
        repo = node.get("repository")
        if not isinstance(repo, dict) or repo.get("isPrivate") is not False:
            raise Fatal("private repository in a public-only result set - aborting")
        owner = (repo.get("owner") or {}).get("login") or ""
        if owner.lower() in OWNER_DENYLIST:
            raise Fatal("denylisted owner in a public-only result set - aborting")


def assert_query_is_public(q: str) -> None:
    """GUARD (b): a query without `is:public` never leaves this process. The
    same query with a privileged token returns private items."""
    if "is:public" not in q.split():
        raise Fatal("search query without is:public - refusing to run")


def assert_no_forbidden_dashes(block: str) -> None:
    """The owner's text rule, output side: whatever path produced a string,
    an em or en dash never reaches the published README. Every foreign title
    is normalised on the way in, so a hit here is OUR defect."""
    if any(dash in block for dash in FORBIDDEN_DASHES) or FORBIDDEN_DASH_ENTITIES.search(block):
        raise Fatal("em or en dash in the rendered block - aborting")


def assert_no_denylisted_owner(block: str) -> None:
    """GUARD (d), output side: the last line of defence, checked on the fully
    rendered text so no future rendering path can bypass the node-side check."""
    lowered = block.lower()
    for owner in OWNER_DENYLIST:
        if owner in lowered:
            raise Fatal("denylisted owner in the rendered block - aborting")


# ── selection ────────────────────────────────────────────────────────────────

def _labels(node: dict) -> list[str]:
    nodes = (node.get("labels") or {}).get("nodes")
    return [str(label.get("name")) for label in nodes or [] if isinstance(label, dict)]


def non_fork(nodes: list[dict]) -> list[dict]:
    """Forks are excluded ENTIRELY, not "labelled separately": a merge into
    somebody's fork is not an upstream merge, and an explanation of that
    distinction inside the block produces the very misreading it tries to
    prevent. isFork must be explicitly false - an absent field excludes."""
    return [n for n in nodes if (n.get("repository") or {}).get("isFork") is False]


def pick_open(items: list[dict]) -> list[dict]:
    opens = [n for n in items if n.get("state") == "OPEN"
             and not any(label.lower() in LABEL_DENYLIST for label in _labels(n))]
    # Two stable passes = (updatedAt desc, nameWithOwner asc, number asc).
    opens.sort(key=lambda n: (n["repository"]["nameWithOwner"], n["number"]))
    opens.sort(key=lambda n: n["updatedAt"], reverse=True)
    return opens[:MAX_OPEN_ITEMS]


def _merged_by_other(node: dict) -> bool:
    """A merge counts only when someone OTHER than the author pressed the
    button; an unknown merger (mergedBy == null) does not count either."""
    merger = node.get("mergedBy")
    login = merger.get("login") if isinstance(merger, dict) else None
    return bool(node.get("mergedAt")) and isinstance(login, str) and bool(login) \
        and login.lower() != AUTHOR


def pick_merged(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Per repository, ALL counted merges, newest first (mergedAt desc, then
    number desc). The renderer shows the count and only the head of the list."""
    by_repo: dict[str, list[dict]] = {}
    for node in items:
        if _merged_by_other(node):
            by_repo.setdefault(node["repository"]["nameWithOwner"], []).append(node)
    for prs in by_repo.values():
        prs.sort(key=lambda p: p["number"], reverse=True)
        prs.sort(key=lambda p: str(p["mergedAt"]), reverse=True)
    ordered = sorted(by_repo.items(),
                     key=lambda kv: (-int(kv[1][0]["repository"].get("stargazerCount") or 0), kv[0]))
    return ordered[:MAX_MERGED_REPOS]


# ── rendering ────────────────────────────────────────────────────────────────

def kmag(stars: int) -> str:
    """Star count as a floored magnitude class, never the raw number: a raw
    count is a daily-changing foreign number (commit noise), while "66k" moves
    roughly every seven weeks and carries the whole point."""
    return "%dk" % (stars // 1000) if stars >= 1000 else ""


def normalize_dashes(text: str) -> str:
    """Foreign text (titles by third parties) may carry em or en dashes; the
    owner's text rule forbids them in anything this script publishes. Their
    HTML entity forms count too, because GitHub renders them as the dash."""
    text = DASH_ENTITIES.sub("-", text)
    for dash in DASHES:
        text = text.replace(dash, "-")
    return text


def shorten(text: str, limit: int = MAX_TITLE) -> str:
    collapsed = " ".join(normalize_dashes(text).replace("`", "").split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "\u2026"
    return collapsed


def esc_md(text: str) -> str:
    """Escape what can break Markdown or open an HTML tag. `/`, `#` and `.` are
    deliberately left alone - in a README file `#23091` is not an autolink."""
    for char in ("\\", "*", "_", "[", "]", "<", ">", "|"):
        text = text.replace(char, "\\" + char)
    return text


def _npm_lines(npm: dict) -> list[str]:
    lines = ["**%s npm downloads in the last 12 months** across %d published packages, "
             "%s of them in the last 30 days:"
             % (format(npm["year"], ","), npm["count"], format(npm["month"], ","))]
    if npm["named"]:
        lines.append(" ·\n".join(
            "[`%s`](https://github.com/%s/%s) %s"
            % (p["name"], AUTHOR, p["repo"], format(p["year"], ","))
            for p in npm["named"]) + ".")
    # The gap is EXPLAINED rather than hidden: a reader who adds the named
    # figures up and misses the total would otherwise distrust the number.
    # The sentence states only the naming rule, never a reason per package:
    # "unnamed" can mean private, foreign owner, no repository field or not
    # yet indexed, and the code proves none of these individually.
    if npm["unnamed_count"] == 1:
        lines.append("1 further package accounts for the remaining %s. It is counted but not "
                     "named, because only packages with a verified public repository are named."
                     % format(npm["unnamed_year"], ","))
    elif npm["unnamed_count"] > 1:
        lines.append("%d further packages account for the remaining %s. They are counted but "
                     "not named, because only packages with a verified public repository are named."
                     % (npm["unnamed_count"], format(npm["unnamed_year"], ",")))
    lines.append(NPM_DISCIPLINE)
    return lines


def _merged_lines(items: list[dict]) -> list[str]:
    merged = pick_merged(items)
    if not merged:
        return []                      # no empty rubric, no placeholder, no "n/a"
    lines = ["**Merged upstream**", ""]
    for repo, prs in merged:
        info = prs[0]["repository"]
        magnitude = kmag(int(info.get("stargazerCount") or 0))
        mergers = sorted({p["mergedBy"]["login"] for p in prs
                          if isinstance(p.get("mergedBy"), dict) and p["mergedBy"].get("login")})
        who = ""
        if len(mergers) == 1:
            # Identity instead of a number, and "the repository owner" only when
            # the payload itself says so.
            who = " by [`%s`](https://github.com/%s)" % (mergers[0], mergers[0])
            if mergers[0] == (info.get("owner") or {}).get("login"):
                who += " (the repository owner)"
        lines.append("- [`%s`](https://github.com/%s)%s: %d pull request%s merged%s%s"
                     % (repo, repo, " (%s stars)" % magnitude if magnitude else "",
                        len(prs), "s" if len(prs) != 1 else "", who,
                        ", newest first:" if len(prs) > 1 else ":"))
        # At most MAX_NEWEST_MERGED title lines per repository: once a repository
        # has that many counted merges, the next one raises the count above and
        # replaces the oldest title here instead of adding a line. Below that,
        # each merge adds one title line.
        for pr in prs[:MAX_NEWEST_MERGED]:
            lines.append("  - [#%d](%s) %s" % (pr["number"], pr["url"], esc_md(shorten(pr["title"]))))
        lines.append("  - [full list](https://github.com/%s/pulls?q=%s)"
                     % (repo, urllib.parse.quote_plus("is:pr is:merged author:%s" % AUTHOR)))
    lines.append("")
    return lines


def _open_lines(items: list[dict]) -> list[str]:
    opens = pick_open(items)
    if not opens:
        return []
    lines = ["**Open right now**", ""]
    for node in opens:
        names = [n for n in _labels(node) if n.lower() in LABEL_ALLOWLIST]
        names.sort(key=lambda n: LABEL_ALLOWLIST.index(n.lower()))
        # "labelled", never "labels:" - no completeness is claimed, and routing
        # labels (area:*, P3, stale, sweeper:*) are none of a reader's business.
        labelled = (" · labelled " + ", ".join("`%s`" % n for n in names)) if names else ""
        lines.append("- [`%s#%d`](%s): %s%s · updated %s"
                     % (node["repository"]["nameWithOwner"], node["number"], node["url"],
                        esc_md(shorten(node["title"])), labelled, node["updatedAt"][:10]))
    lines.append("")
    return lines


def _attribution_line(items: list[dict], attribution: dict[str, int]) -> list[str]:
    closed = attribution["closed"]
    if not closed:
        return []
    open_prs = sum(1 for n in items
                   if n.get("__typename") == "PullRequest" and n.get("state") == "OPEN"
                   and n["repository"]["nameWithOwner"] == ATTRIBUTION_REPO)
    bits = []
    if attribution["bot"]:
        bits.append("%d by the repository's own automation (`%s`)"
                    % (attribution["bot"], AUTOMATION_LOGIN))
    if attribution["self"]:
        bits.append("%d by me" % attribution["self"])
    bits.append("%d by someone else" % attribution["other"] if attribution["other"]
                else "none by a maintainer")
    return ["On [`%s`](https://github.com/%s/pulls?q=is%%3Apr+author%%3A%s) %d of my %d "
            "pull requests are closed: %s."
            % (ATTRIBUTION_REPO, ATTRIBUTION_REPO, AUTHOR, closed, closed + open_prs,
               ", ".join(bits))]


def render_story(items: list[dict], attribution: dict[str, int], npm: dict) -> str:
    lines = [LANG_NOTE]
    lines.extend(_npm_lines(npm))
    repos = sorted({n["repository"]["nameWithOwner"] for n in items})
    if repos:
        # Breadth, not volume: "110 items, 100 of them closed" reads to a
        # maintainer as mass submission, while the repository count reads as reach.
        lines.extend(["", "**Contributions to %d upstream repositories I do not own.**" % len(repos), ""])
    lines.extend(_merged_lines(items))
    lines.extend(_open_lines(items))
    lines.extend(_attribution_line(items, attribution))
    body = "\n".join(lines).rstrip("\n")
    return "%s\n%s\n%s" % (STORY_START, body, STORY_END)


def render_fresh(now: datetime, run: dict[str, str] | None, degraded: str | None) -> str:
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    if degraded:
        parts = ["Last generator run <strong>%s</strong>" % stamp]
    else:
        parts = ["Regenerated <strong>%s</strong> from %s and %s"
                 % (stamp, GITHUB_SOURCE, NPM_SOURCE)]
    if run:
        parts.append('<a href="%s">run #%s</a>' % (run["url"], run["number"]))
    if degraded:
        parts.append("<strong>degraded</strong>: %s, the section above is unchanged "
                     "from an earlier run" % degraded)
    parts.append(SCHEDULE_NOTE)
    return "%s\n<sub>:arrows_counterclockwise: %s</sub>\n%s" % (
        FRESH_START, " · ".join(parts), FRESH_END)


# ── README surgery ───────────────────────────────────────────────────────────

def _marker_span(text: str, start: str, end: str) -> tuple[int, int]:
    if text.count(start) != 1 or text.count(end) != 1:
        raise Fatal("README needs exactly one %s / %s pair" % (start, end))
    first, last = text.index(start), text.index(end)
    if last < first:
        raise Fatal("README has %s before %s" % (end, start))
    return first, last + len(end)


def replace_block(text: str, start: str, end: str, block: str) -> str:
    first, last = _marker_span(text, start, end)
    return text[:first] + block + text[last:]


def run_info() -> dict[str, str] | None:
    """Run metadata, or None for a local run - a run link is never invented."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    number = os.environ.get("GITHUB_RUN_NUMBER")
    if not (server and repository and run_id and number):
        return None
    return {"url": "%s/%s/actions/runs/%s" % (server, repository, run_id), "number": number}


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise Fatal("--now must be an ISO 8601 UTC timestamp like 2026-07-25T05:32:00Z") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the STORY and FRESH blocks of the profile README "
                    "from public GitHub and npm data.")
    parser.add_argument("--readme", default="README.md", type=Path,
                        help="README to rewrite in place (default: README.md)")
    parser.add_argument("--now", default=None,
                        help="run timestamp as ISO 8601 UTC (e.g. 2026-07-25T05:32:00Z); "
                             "defaults to the current time")
    args = parser.parse_args(argv)
    _STATS.update(cost=0, pages=0, npm_ok=0)
    try:
        now = _parse_now(args.now)
        readme: Path = args.readme
        try:
            text = readme.read_text(encoding="utf-8")
        except OSError as exc:
            raise Fatal("cannot read %s (%s)" % (readme, exc.__class__.__name__)) from None
        # Both marker pairs are validated BEFORE any request: a broken README is
        # our defect, and finding it costs nothing.
        _marker_span(text, STORY_START, STORY_END)
        _marker_span(text, FRESH_START, FRESH_END)
        tok = token()

        story: str | None = None
        degraded: str | None = None
        items: list[dict] = []
        try:
            nodes = fetch_items(tok)
            assert_public(nodes)                                  # GUARDS (c)+(d)
            items = non_fork(nodes)
            attribution = fetch_close_attribution(tok)
            closed_here = sum(1 for n in items
                              if n.get("__typename") == "PullRequest"
                              and n.get("state") == "CLOSED"
                              and n["repository"]["nameWithOwner"] == ATTRIBUTION_REPO)
            if attribution["closed"] != closed_here:
                log("%s: %d closed PRs in the attribution query vs %d in the main query"
                    % (GITHUB_SOURCE, attribution["closed"], closed_here))
                raise Degraded("%s returned an inconsistent snapshot" % GITHUB_SOURCE)
            packages = discover_packages()
            public_repos = fetch_public_repos(tok)
            npm = collect_npm(packages, public_repos)
            story = render_story(items, attribution, npm)
            assert_no_denylisted_owner(story)                     # GUARD (d), output side
            assert_no_forbidden_dashes(story)
        except Degraded as exc:
            degraded = str(exc)

        new = text
        if story is not None:
            new = replace_block(new, STORY_START, STORY_END, story)
        fresh = render_fresh(now, run_info(), degraded)
        assert_no_forbidden_dashes(fresh)
        new = replace_block(new, FRESH_START, FRESH_END, fresh)
        if new != text:
            readme.write_text(new, encoding="utf-8")              # THE ONLY WRITE
            log("README block updated")
        else:
            log("no change")
        if degraded:
            error("degraded: %s; the generated section is unchanged" % degraded)
            return 0
        repos = len({n["repository"]["nameWithOwner"] for n in items})
        log("%d items in %d upstream repos, graphql cost %d, npm %d/%d ok"
            % (len(items), repos, _STATS["cost"], _STATS["npm_ok"], npm["count"]))
        return 0
    except Fatal as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
