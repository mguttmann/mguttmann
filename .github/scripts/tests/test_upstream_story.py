#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Unit tests for upstream_story.py, stdlib unittest only.
#
# ONE in-process mock server (ThreadingHTTPServer on an ephemeral port) plays
# both upstreams: POST /graphql for the GitHub search API and
# GET /downloads/point/<window>/<package> for api.npmjs.org. story.GITHUB_GRAPHQL
# and story.NPM_API are patched to point at it, so every assertion below runs
# over a REAL urllib round trip against 127.0.0.1, the same technique as
# test_graphql_split_proxy.py, and the only one that is green both locally (no
# CA trust for real HTTPS) and in CI (no network at all).
#
# The mock records every request it receives (path, headers and body), which is
# what makes the four privacy guards testable from the outside instead of by
# reading the source.
#
# FIXTURES ARE REAL. Every number, title, label, star count, timestamp, login
# and download figure below was fetched live on 2026-07-25 with the query
# documents from upstream_story.py; only the repeated JSON scaffolding is
# factored into the helpers _repo/_pr/_issue. Re-measure with:
#   gh api graphql -F q='author:mguttmann is:public -user:mguttmann sort:updated-desc' \
#                  -F query=@main.graphql
#   curl -s https://api.npmjs.org/downloads/point/last-year/opencode-sonarqube
#
# Run:  cd .github/scripts/tests && python3 -m unittest discover -p 'test_*.py'
# ---------------------------------------------------------------------------

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import upstream_story as story  # noqa: E402

NOW = "2026-07-25T05:32:00Z"
RUN_ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "mguttmann/mguttmann",
    "GITHUB_RUN_ID": "30145867690",
    "GITHUB_RUN_NUMBER": "44",
}
SCRUBBED = ("GH_TOKEN", "GITHUB_TOKEN", "STATS_TOKEN", "GITHUB_SERVER_URL",
            "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_NUMBER")

# ── real payload fixtures ───────────────────────────────────────────────────

OPENCODE = {"nameWithOwner": "anomalyco/opencode", "isFork": False, "isPrivate": False,
            "stargazerCount": 189513, "owner": {"login": "anomalyco"}}
CLAUDE_CODE = {"nameWithOwner": "anthropics/claude-code", "isFork": False, "isPrivate": False,
               "stargazerCount": 139007, "owner": {"login": "anthropics"}}
HERMES = {"nameWithOwner": "NousResearch/hermes-agent", "isFork": False, "isPrivate": False,
          "stargazerCount": 220182, "owner": {"login": "NousResearch"}}
OPENAGENT = {"nameWithOwner": "code-yeongyu/oh-my-openagent", "isFork": False, "isPrivate": False,
             "stargazerCount": 66565, "owner": {"login": "code-yeongyu"}}
VASYA_FORK = {"nameWithOwner": "VasyaYovbak/opencode", "isFork": True, "isPrivate": False,
              "stargazerCount": 0, "owner": {"login": "VasyaYovbak"}}
TYPEWHISPER_MAC = {"nameWithOwner": "TypeWhisper/typewhisper-mac", "isFork": False,
                   "isPrivate": False, "stargazerCount": 1625, "owner": {"login": "TypeWhisper"}}
TYPEWHISPER_PLUGINS = {"nameWithOwner": "TypeWhisper/typewhisper-plugins", "isFork": False,
                       "isPrivate": False, "stargazerCount": 1, "owner": {"login": "TypeWhisper"}}
OPENCLAW = {"nameWithOwner": "openclaw/openclaw", "isFork": False, "isPrivate": False,
            "stargazerCount": 384076, "owner": {"login": "openclaw"}}
PAPERCLIP = {"nameWithOwner": "paperclipai/paperclip", "isFork": False, "isPrivate": False,
             "stargazerCount": 74684, "owner": {"login": "paperclipai"}}
CLAUDE_MEM = {"nameWithOwner": "thedotmack/claude-mem", "isFork": False, "isPrivate": False,
              "stargazerCount": 88509, "owner": {"login": "thedotmack"}}
PAPERCLIP_PLUGIN = {"nameWithOwner": "mvanhorn/paperclip-plugin-github-issues", "isFork": False,
                    "isPrivate": False, "stargazerCount": 26, "owner": {"login": "mvanhorn"}}


def _pr(number, title, repo, state, updated, merged_at=None, merged_by=None, labels=()):
    return {"__typename": "PullRequest", "number": number, "title": title,
            "url": "https://github.com/%s/pull/%d" % (repo["nameWithOwner"], number),
            "state": state, "updatedAt": updated, "mergedAt": merged_at,
            "mergedBy": {"login": merged_by} if merged_by else None,
            "labels": {"nodes": [{"name": name} for name in labels]},
            "repository": repo}


def _issue(number, title, repo, state, updated, labels=()):
    return {"__typename": "Issue", "number": number, "title": title,
            "url": "https://github.com/%s/issues/%d" % (repo["nameWithOwner"], number),
            "state": state, "updatedAt": updated,
            "labels": {"nodes": [{"name": name} for name in labels]},
            "repository": repo}


# The 24 closed, unmerged pull requests on anomalyco/opencode (number, updatedAt, title).
CLOSED_OPENCODE = (
    (32396, "2026-07-15T22:23:22Z", "docs(workflow): workflows guide (6/6) \u2014 split from #29789"),
    (32395, "2026-07-15T22:23:00Z", "feat(workflow): ctx.tool + plugin registration (5/6) \u2014 split from #29789"),
    (32394, "2026-07-15T22:22:38Z", "feat(workflow): web app + desktop (4/6) \u2014 split from #29789"),
    (32393, "2026-07-15T22:22:15Z", "feat(workflow): TUI workflow dialogs (3/6) \u2014 split from #29789"),
    (32392, "2026-07-15T22:21:53Z", "feat(workflow): server routes + SDK (2/6) \u2014 split from #29789"),
    (32390, "2026-07-15T22:21:30Z", "feat(workflow): engine-core (1/6) \u2014 modularized workflow engine, split from #29789"),
    (32167, "2026-06-14T12:23:31Z", "feat: nested sub-agent spawning (up to 5 levels) + multi-agent workflow orchestration"),
    (14871, "2026-02-24T09:47:32Z", "fix(win32): make all unit tests pass on Windows"),
    (13457, "2026-02-13T10:42:42Z", "feat: enable 1M context window for Anthropic Claude models"),
    (12270, "2026-05-06T06:50:06Z", "fix: handle custom tool import failures gracefully in registry"),
    (12000, "2026-05-06T06:50:03Z", "feat(desktop): web server mirror \u2014 true 1:1 remote access via TCP reverse proxy"),
    (11855, "2026-05-06T06:49:59Z", "feat(app): add project search to command palette"),
    (11833, "2026-05-06T06:49:56Z", "feat(permission): add YOLO mode to auto-approve all permission prompts"),
    (11832, "2026-05-26T06:47:41Z", "feat(auth): multi-account OAuth support with auto-relogin"),
    (11758, "2026-05-06T06:49:52Z", "feat(app): add dynamic sidebar sorting for active projects and sessions"),
    (10310, "2026-05-06T06:49:48Z", "feat(desktop): add setting to open links in external browser"),
    (9972, "2026-07-17T04:17:22Z", "feat: OAuth Enhancements - Multi-Account, YOLO Mode, Auto-Relogin"),
    (9455, "2026-01-22T07:28:04Z", "feat(auth): Auto-Relogin via Persistent Browser Sessions"),
    (9122, "2026-01-19T06:36:09Z", "feat(auth): add OAuth token keepalive via Messages API"),
    (9112, "2026-03-11T09:56:09Z", "feat: add OAuth token keep-alive to prevent expiration"),
    (9073, "2026-05-09T16:52:48Z", "feat: YOLO Mode - Skip All Permission Prompts (CLI + Desktop)"),
    (9069, "2026-01-22T07:28:05Z", "feat: Multi-Account OAuth Rotation with Settings UI and CLI Enhancements"),
    (8949, "2026-01-17T08:20:13Z", "fix: remove invalid step-start parts from UIMessage conversion"),
    (8912, "2026-01-17T08:40:55Z", "feat(app): Add OAuth rate limits and usage dashboard"),
)
# Which of them the repository's own automation closed (the rest: the author).
BOT_CLOSED = (32396, 32395, 32394, 32393, 32392, 32390, 14871,
              12270, 12000, 11855, 11833, 11758, 10310)

OPEN_32301 = _pr(32301, "feat: nested sub-agent spawning (up to 5 levels) + fixes for #23091 / #13715",
                 OPENCODE, "OPEN", "2026-07-24T13:45:03Z")
OPEN_77704 = _issue(
    77704,
    "[BUG] Custom remote MCP connectors intermittently lose all tools / aggregate tool list "
    "capped at exactly 256 across connectors \u2014 regression since ~mid-July 2026 "
    "(web + desktop, org + personal accounts)",
    CLAUDE_CODE, "OPEN", "2026-07-24T02:23:21Z", labels=("bug", "area:mcp"))
OPEN_59942 = _issue(
    59942,
    "[Bug] OTel `query_source` attribute missing on `api_request` events and "
    "`cost.usage`/`token.usage` metric counters since 2026-05-13 "
    "(affects both claude-code and cowork)",
    CLAUDE_CODE, "OPEN", "2026-07-19T10:32:21Z",
    labels=("bug", "has repro", "area:core", "area:cowork", "regression",
            "area:agent-sdk", "stale"))
OPEN_64962 = _pr(64962, "apps/mobile: iOS client bundling the desktop renderer (remote-gateway)",
                 HERMES, "OPEN", "2026-07-16T06:59:23Z",
                 labels=("type/feature", "P3", "sweeper:risk-session-state",
                         "sweeper:risk-message-delivery", "sweeper:risk-security-boundary",
                         "sweeper:risk-compatibility", "sweeper:blast-contained", "comp/desktop"))
OPEN_64961 = _issue(64961, "Proposal: first-class mobile (iOS) client for Hermes",
                    HERMES, "OPEN", "2026-07-15T12:29:21Z",
                    labels=("duplicate", "type/feature", "P3", "comp/desktop"))
OPEN_54697 = _pr(54697, "feat(webhook): verify native Asana webhook signatures (X-Hook-Signature)",
                 HERMES, "OPEN", "2026-07-15T09:25:40Z",
                 labels=("type/feature", "comp/gateway", "platform/webhook", "P3",
                         "sweeper:risk-message-delivery", "sweeper:risk-security-boundary",
                         "sweeper:blast-moderate"))
MERGED_4121 = _pr(4121, "fix(delegate-task): default run_in_background and load_skills instead of "
                        "throwing (fixes #4119)",
                  OPENAGENT, "MERGED", "2026-05-20T15:01:13Z",
                  merged_at="2026-05-20T15:01:12Z", merged_by="code-yeongyu")
MERGED_4122 = _pr(4122, "fix(background-agent): defer parent-wake when a user message just "
                        "arrived (fixes #4120)",
                  OPENAGENT, "MERGED", "2026-05-19T07:14:20Z",
                  merged_at="2026-05-18T02:06:01Z", merged_by="code-yeongyu")
# Merged, but into a FORK of opencode, which is never an upstream merge.
FORK_MERGED_1 = _pr(1, "fix: real cancellation, crash recovery, resilience, budgets and test "
                       "coverage for dynamic workflows",
                    VASYA_FORK, "MERGED", "2026-06-05T19:30:00Z",
                    merged_at="2026-06-05T19:30:00Z", merged_by="VasyaYovbak")
FORK_MERGED_2 = _pr(2, "Workflows rolling follow-up: dev sync, audit round 2, Deep Research example",
                    VASYA_FORK, "MERGED", "2026-06-07T08:05:12Z",
                    merged_at="2026-06-07T08:05:12Z", merged_by="VasyaYovbak",
                    labels=("needs:compliance", "needs:title"))
FORK_CLOSED_3 = _pr(3, "feat(workflow): question/shell/nesting/isolation/events \u2014 full "
                       "review-roadmap round",
                    VASYA_FORK, "CLOSED", "2026-06-13T12:29:40Z")


def main_nodes():
    """The full main-query result set, in the order the API returns it."""
    nodes = [OPEN_32301, OPEN_77704, OPEN_59942, OPEN_64962, OPEN_64961, OPEN_54697,
             _issue(32166, "[FEATURE]: Nested sub-agent spawning (up to 5 levels) + multi-agent "
                           "workflow orchestration", OPENCODE, "OPEN", "2026-06-23T05:59:17Z"),
             MERGED_4121, MERGED_4122,
             FORK_CLOSED_3, FORK_MERGED_2, FORK_MERGED_1,
             _issue(677, "Dictation pastes previous clipboard instead of transcription in "
                         "terminals \u2014 preserveClipboard restore races synthetic paste "
                         "(regression after #635 / #630)",
                    TYPEWHISPER_MAC, "CLOSED", "2026-06-06T21:20:45Z"),
             _pr(1, "Add Claude (OAuth Pro/Max) LLM plugin", TYPEWHISPER_PLUGINS, "CLOSED",
                 "2026-05-22T16:34:33Z"),
             _issue(26287, "Talk Mode: interruptOnSpeech not working on macOS (2026.2.24)",
                    OPENCLAW, "CLOSED", "2026-04-09T04:15:01Z", labels=("stale",)),
             _issue(3394, "Plugin tool dispatcher: pluginDbId never threaded through, every "
                          "external plugin's agent.tools.execute returns 502",
                    PAPERCLIP, "CLOSED", "2026-06-04T02:13:23Z"),
             _issue(2566, "MCP server has no grammar-introspection routes + worker provider "
                          "lacks retry telemetry + no audit log",
                    CLAUDE_MEM, "CLOSED", "2026-06-05T01:32:23Z"),
             _pr(6, "feat: multi-project auto-discovery + full-mirror bidirectional sync",
                 PAPERCLIP_PLUGIN, "CLOSED", "2026-05-26T06:48:24Z"),
             _issue(5, "Plugin incompatible with current Paperclip SDK + missing full-mirror "
                       "sync mode", PAPERCLIP_PLUGIN, "OPEN", "2026-04-11T16:01:19Z")]
    nodes.extend(_pr(number, title, OPENCODE, "CLOSED", updated)
                 for number, updated, title in CLOSED_OPENCODE)
    return nodes


def attribution_nodes(other=(), unknown=()):
    """The attribution result set: one node per closed, unmerged opencode PR."""
    nodes = []
    for number, _updated, _title in CLOSED_OPENCODE:
        if number in unknown:
            actor = None
        elif number in other:
            actor = {"login": "some-maintainer", "__typename": "User"}
        elif number in BOT_CLOSED:
            actor = {"login": "github-actions", "__typename": "Bot"}
        else:
            actor = {"login": "mguttmann", "__typename": "User"}
        nodes.append({"number": number,
                      "repository": {"nameWithOwner": "anomalyco/opencode", "isFork": False,
                                     "isPrivate": False, "owner": {"login": "anomalyco"}},
                      "timelineItems": {"nodes": [{"actor": actor}]}})
    return nodes


NPM_POINTS = {
    "opencode-sonarqube": (843, 16667),
    "the-real-snipeit-mcp": (61, 559),
    "the-real-unifi-mcp": (61, 577),
    "@mguttmann/hetzner-cloud-mcp": (60, 433),
    "the-real-bitwarden-mcp": (37, 378),
    # Added 2026-09-28 (live: last-month 13, last-year 224). The July table
    # did not know this package, which is the defect the registry search fixes.
    "opencode-abstraction-scanner": (13, 224),
}


def _registry_object(name, repo_url, maintainers=("m_guttmann",)):
    """One `objects[]` entry of registry.npmjs.org/-/v1/search, reduced to the
    fields the script reads (shape verified live on 2026-09-28)."""
    links = {"npm": "https://www.npmjs.com/package/%s" % name}
    if repo_url:
        links["repository"] = repo_url
    return {"package": {"name": name, "links": links,
                        "maintainers": [{"username": m} for m in maintainers]}}


def registry_objects():
    """The live registry search result of 2026-09-28: six packages, three of
    them with a private repository (HTTP 404 for the public)."""
    return [_registry_object(name, "git+https://github.com/mguttmann/%s.git" % repo)
            for name, repo in (("opencode-sonarqube", "opencode-sonarqube"),
                               ("the-real-snipeit-mcp", "the-real-snipeit-mcp"),
                               ("the-real-unifi-mcp", "the-real-unifi-mcp"),
                               ("@mguttmann/hetzner-cloud-mcp", "the-real-hetzner-mcp"),
                               ("the-real-bitwarden-mcp", "the-real-bitwarden-mcp"),
                               ("opencode-abstraction-scanner", "opencode-abstraction-scanner"))]


def registry_response(objects, total=None):
    return json.dumps({"objects": objects,
                       "total": len(objects) if total is None else total,
                       "time": "2026-09-28T08:00:00.000Z"}).encode()


def _public_repo(name):
    return {"name": name, "isPrivate": False, "owner": {"login": "mguttmann"}}


# The `user:mguttmann is:public fork:true` repository search (live 2026-09-28,
# reduced to the names that matter plus two bystanders). The three private
# package repositories are absent, exactly as on the live API.
PUBLIC_REPOS = [_public_repo(name) for name in (
    "mguttmann", "opencode-sonarqube", "the-real-bitwarden-mcp",
    "the-real-hetzner-mcp", "code-audit-suite")]

# The verbatim expected output: the design spec's binding "SOLL" block, with
# the three changes of the September ticket (six discovered packages, the
# merged block as count + newest three + full-list link, no em or en dashes).
GOLDEN = """<!-- STORY:START -->
<!-- LANG: prose below is GENERATED, translate for a DE variant; every number is fetched at run time. -->
**18,838 npm downloads in the last 12 months** across 6 published packages, 1,075 of them in the last 30 days:
[`opencode-sonarqube`](https://github.com/mguttmann/opencode-sonarqube) 16,667 ·
[`@mguttmann/hetzner-cloud-mcp`](https://github.com/mguttmann/the-real-hetzner-mcp) 433 ·
[`the-real-bitwarden-mcp`](https://github.com/mguttmann/the-real-bitwarden-mcp) 378.
3 further packages account for the remaining 1,360. They are counted but not named, because only packages with a verified public repository are named.
<sub>Downloads, not users: CI and mirror traffic is included. Source: api.npmjs.org.</sub>

**Contributions to 10 upstream repositories I do not own.**

**Merged upstream**

- [`code-yeongyu/oh-my-openagent`](https://github.com/code-yeongyu/oh-my-openagent) (66k stars): \
2 pull requests merged by [`code-yeongyu`](https://github.com/code-yeongyu) (the repository owner), newest first:
  - [#4121](https://github.com/code-yeongyu/oh-my-openagent/pull/4121) fix(delegate-task): \
default run\\_in\\_background and load\\_skills instead of throwing (fixes #4119)
  - [#4122](https://github.com/code-yeongyu/oh-my-openagent/pull/4122) fix(background-agent): \
defer parent-wake when a user message just arrived (fixes #4120)
  - [full list](https://github.com/code-yeongyu/oh-my-openagent/pulls?q=is%3Apr+is%3Amerged+author%3Amguttmann)

**Open right now**

- [`anomalyco/opencode#32301`](https://github.com/anomalyco/opencode/pull/32301): feat: nested \
sub-agent spawning (up to 5 levels) + fixes for #23091 / #13715 · updated 2026-07-24
- [`anthropics/claude-code#77704`](https://github.com/anthropics/claude-code/issues/77704): \
\\[BUG\\] Custom remote MCP connectors intermittently lose all tools / aggregate tool list \
capped\u2026 · labelled `bug` · updated 2026-07-24
- [`anthropics/claude-code#59942`](https://github.com/anthropics/claude-code/issues/59942): \
\\[Bug\\] OTel query\\_source attribute missing on api\\_request events and cost.usage/token.usage\u2026 \
· labelled `bug`, `has repro`, `regression` · updated 2026-07-19

On [`anomalyco/opencode`](https://github.com/anomalyco/opencode/pulls?q=is%3Apr+author%3Amguttmann) \
24 of my 25 pull requests are closed: 13 by the repository's own automation (`github-actions`), \
11 by me, none by a maintainer.
<!-- STORY:END -->
<!-- FRESH:START -->
<sub>:arrows_counterclockwise: Regenerated <strong>2026-07-25 05:32 UTC</strong> from the GitHub \
API and api.npmjs.org · <a href="https://github.com/mguttmann/mguttmann/actions/runs/30145867690">\
run #44</a> · scheduled daily at 04:30 UTC</sub>
<!-- FRESH:END -->"""

OLD_STORY = "STALE CONTENT that a degraded run must leave byte-identical: 25 PRs, 1 open."
README_TEMPLATE = """# mguttmann

## :wave: About me

Hero content above the fold, never touched by this script.

## :handshake: Open-source work

<!-- STORY:START -->
%s
<!-- STORY:END -->
<!-- FRESH:START -->
<sub>:arrows_counterclockwise: previous stamp</sub>
<!-- FRESH:END -->

## :mailbox_with_mail: Reach me
""" % OLD_STORY


# ── mock upstream (GitHub GraphQL + api.npmjs.org in one server) ─────────────

def search_response(nodes, after, page_size=1000):
    start = int(after) if after else 0
    page = nodes[start:start + page_size]
    end = start + len(page)
    return json.dumps({"data": {
        "rateLimit": {"cost": 1, "remaining": 4587},
        "search": {"issueCount": len(nodes),
                   "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)},
                   "nodes": page}}}).encode()


def default_graphql(payload):
    variables = (payload or {}).get("variables") or {}
    query = variables.get("q")
    if query == story.SEARCH_ALL:
        return 200, search_response(main_nodes(), variables.get("after"))
    if query == story.SEARCH_CLOSED:
        return 200, search_response(attribution_nodes(), variables.get("after"))
    if query == story.SEARCH_REPOS:
        return 200, search_response(PUBLIC_REPOS, variables.get("after"))
    return 200, json.dumps({"errors": [{"type": "INVALID",
                                        "message": "unexpected query in test"}]}).encode()


def default_npm(window, name):
    month, year = NPM_POINTS[name]
    return 200, json.dumps({"downloads": month if window == "last-month" else year,
                            "start": "2026-06-25", "end": "2026-07-25",
                            "package": name}).encode()


class MockHandler(BaseHTTPRequestHandler):
    requests: list = []                 # [{"method","path","headers","body","payload"}]
    graphql_responder = staticmethod(default_graphql)
    npm_responder = staticmethod(default_npm)
    registry_responder = staticmethod(lambda query: (200, registry_response(registry_objects())))

    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        pass

    def _record(self, body, payload):
        MockHandler.requests.append({
            "method": self.command, "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body, "payload": payload})

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        self._record(body, payload)
        if self.path != "/graphql":
            self._send(404, b'{"message":"not found"}')
            return
        status, out = MockHandler.graphql_responder(payload)
        self._send(status, out)

    def do_GET(self):
        self._record(b"", None)
        if self.path.startswith("/-/v1/search?"):
            status, out = MockHandler.registry_responder(self.path.partition("?")[2])
            self._send(status, out)
            return
        prefix = "/downloads/point/"
        if not self.path.startswith(prefix):
            self._send(404, b'{"error":"not found"}')
            return
        window, _, name = self.path[len(prefix):].partition("/")
        status, out = MockHandler.npm_responder(window, name)
        self._send(status, out)


class StoryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        port = cls.mock.server_address[1]
        cls._orig = (story.GITHUB_GRAPHQL, story.NPM_API, story.NPM_SEARCH, story.RETRY_BACKOFF)
        story.GITHUB_GRAPHQL = "http://127.0.0.1:%d/graphql" % port
        story.NPM_API = "http://127.0.0.1:%d/downloads/point" % port
        story.NPM_SEARCH = "http://127.0.0.1:%d/-/v1/search" % port
        # Same NUMBER of attempts as production, without the real waits.
        story.RETRY_BACKOFF = tuple(0.0 for _ in story.RETRY_BACKOFF)

    @classmethod
    def tearDownClass(cls):
        cls.mock.shutdown()
        story.GITHUB_GRAPHQL, story.NPM_API, story.NPM_SEARCH, story.RETRY_BACKOFF = cls._orig

    def setUp(self):
        MockHandler.requests = []
        MockHandler.graphql_responder = staticmethod(default_graphql)
        MockHandler.npm_responder = staticmethod(default_npm)
        MockHandler.registry_responder = staticmethod(
            lambda query: (200, registry_response(registry_objects())))
        self._env = dict(os.environ)
        for key in SCRUBBED:                     # a real Actions runner exports some of these
            os.environ.pop(key, None)
        os.environ["GH_TOKEN"] = "gho_ok"
        self._tmp = tempfile.TemporaryDirectory()
        self.readme = Path(self._tmp.name) / "README.md"
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

    # ── helpers ─────────────────────────────────────────────────────────────

    def run_main(self, *extra, readme=None, now=NOW):
        argv = ["--readme", str(readme or self.readme)]
        if now:
            argv += ["--now", now]
        argv += list(extra)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = story.main(argv)
        return code, buffer.getvalue()

    def graphql_bodies(self):
        return [r for r in MockHandler.requests if r["method"] == "POST"]

    def npm_requests(self):
        """The download-counter requests only (the registry search is separate)."""
        return [r for r in MockHandler.requests
                if r["method"] == "GET" and r["path"].startswith("/downloads/")]

    def registry_requests(self):
        return [r for r in MockHandler.requests
                if r["method"] == "GET" and r["path"].startswith("/-/v1/search")]

    def serve(self, nodes=None, attribution=None, repos=None):
        nodes = main_nodes() if nodes is None else nodes
        attribution = attribution_nodes() if attribution is None else attribution
        repos = PUBLIC_REPOS if repos is None else repos

        def responder(payload):
            variables = (payload or {}).get("variables") or {}
            if variables.get("q") == story.SEARCH_ALL:
                return 200, search_response(nodes, variables.get("after"))
            if variables.get("q") == story.SEARCH_REPOS:
                return 200, search_response(repos, variables.get("after"))
            return 200, search_response(attribution, variables.get("after"))

        MockHandler.graphql_responder = staticmethod(responder)

    def count_readme_writes(self, *args, **kwargs):
        """Run main() and count the REAL filesystem writes to the README.

        Every write entry point is wrapped (Path.write_text, Path.write_bytes,
        Path.open and builtins.open), so the count holds no matter which API the
        production code reaches for, and a nested call (write_text opens the file
        internally) is counted once via the depth guard. This is the behavioural
        version of "exactly one write": grepping the source for `write_text(`
        proves nothing about a future `open(p, "w")`."""
        target = os.path.realpath(str(self.readme))
        writes: list[str] = []
        depth = [0]
        originals = {"wt": pathlib.Path.write_text, "wb": pathlib.Path.write_bytes,
                     "po": pathlib.Path.open, "bo": builtins.open}

        def record(path, mode, label):
            if depth[0] or not any(flag in mode for flag in "wxa+"):
                return
            try:
                same = os.path.realpath(str(path)) == target
            except (TypeError, ValueError):
                same = False
            if same:
                writes.append(label)

        def wrap(label, original, mode_of):
            def wrapper(path, *a, **k):
                record(path, mode_of(a, k), label)
                depth[0] += 1
                try:
                    return original(path, *a, **k)
                finally:
                    depth[0] -= 1
            return wrapper

        pathlib.Path.write_text = wrap("write_text", originals["wt"], lambda a, k: "w")
        pathlib.Path.write_bytes = wrap("write_bytes", originals["wb"], lambda a, k: "w")
        pathlib.Path.open = wrap("Path.open", originals["po"],
                                 lambda a, k: str(a[0] if a else k.get("mode", "r")))
        builtins.open = wrap("open", originals["bo"],
                             lambda a, k: str(a[0] if a else k.get("mode", "r")))
        try:
            result = self.run_main(*args, **kwargs)
        finally:
            pathlib.Path.write_text = originals["wt"]
            pathlib.Path.write_bytes = originals["wb"]
            pathlib.Path.open = originals["po"]
            builtins.open = originals["bo"]
        return result, writes

    def story_block(self, text=None):
        text = self.readme.read_text(encoding="utf-8") if text is None else text
        start = text.index(story.STORY_START)
        return text[start:text.index(story.STORY_END) + len(story.STORY_END)]

    def fresh_block(self):
        text = self.readme.read_text(encoding="utf-8")
        start = text.index(story.FRESH_START)
        return text[start:text.index(story.FRESH_END) + len(story.FRESH_END)]

    # ── T-1 ... T-5: the four privacy guards ──────────────────────────────────

    def test_token_is_gh_token_and_stats_token_is_never_used(self):
        """GUARD (a): only GH_TOKEN reaches the wire, and the privileged secret's
        name does not even occur in the script."""
        os.environ["GH_TOKEN"] = "gho_ok"
        os.environ["STATS_TOKEN"] = "stats_must_not_leak"
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        posts = self.graphql_bodies()
        self.assertTrue(posts)
        for request in posts:
            self.assertEqual(request["headers"].get("authorization"), "Bearer gho_ok")
        # Nothing the mock ever received may carry the privileged token, in any
        # header or any body, including the npm requests.
        for request in MockHandler.requests:
            self.assertNotIn("stats_must_not_leak", json.dumps(request["headers"]))
            self.assertNotIn(b"stats_must_not_leak", request["body"])
        self.assertNotIn("stats_must_not_leak", self.readme.read_text(encoding="utf-8"))
        self.assertNotIn("stats_must_not_leak", out)
        source = Path(story.__file__).read_text(encoding="utf-8")
        self.assertNotIn("STATS_TOKEN", source)
        # GITHUB_TOKEN is the documented fallback and must keep working.
        del os.environ["GH_TOKEN"]
        os.environ["GITHUB_TOKEN"] = "ghs_builtin"
        MockHandler.requests = []
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.graphql_bodies()[0]["headers"].get("authorization"),
                         "Bearer ghs_builtin")

    def test_every_search_query_carries_is_public(self):
        """GUARD (b): asserted on the constants, on the wire and on the guard."""
        for query in (story.SEARCH_ALL, story.SEARCH_CLOSED, story.SEARCH_REPOS):
            self.assertIn("is:public", query.split())
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        posts = self.graphql_bodies()
        self.assertGreaterEqual(len(posts), 2)
        for request in posts:
            sent = request["payload"]["variables"]["q"]
            self.assertIn("is:public", sent.split(), sent)
        with self.assertRaises(story.Fatal):
            story.assert_query_is_public("author:mguttmann")
        with self.assertRaises(story.Fatal):
            story.assert_query_is_public("author:mguttmann is:private")
        # ...and the guard must be WIRED IN, not merely present: the three checks
        # above all still pass if someone deletes the call from search_all, which
        # would leave guard (b) as dead code. Feeding a query that lost `is:public`
        # must abort BEFORE the request leaves the process.
        for constant in ("SEARCH_ALL", "SEARCH_CLOSED", "SEARCH_REPOS"):
            with self.subTest(constant=constant):
                original = getattr(story, constant)
                setattr(story, constant, original.replace(" is:public", ""))
                MockHandler.requests = []
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                before = self.readme.read_bytes()
                try:
                    code, out = self.run_main()
                finally:
                    setattr(story, constant, original)
                self.assertEqual(code, 1, out)
                self.assertIn("is:public", out)
                self.assertEqual(self.readme.read_bytes(), before)
                if constant == "SEARCH_ALL":     # nothing may go out at all
                    self.assertEqual(MockHandler.requests, [])

    def test_private_item_aborts_with_no_partial_render(self):
        """GUARD (c): one private item kills the whole run, no partial render.

        The private item is injected at THREE positions, because position is
        exactly what a sloppy guard gets wrong: as the freshest item it would be
        rendered first, in the middle it must not be skipped over, and last it
        must still be reached. A guard that checked only the head of the list
        would pass the "last" case and still leak."""
        secret = {"nameWithOwner": "journaway-internal/secret-vendor-audit", "isFork": False,
                  "isPrivate": True, "stargazerCount": 0, "owner": {"login": "acme"}}
        # updatedAt is the newest in the whole set, so an unfiltered renderer
        # would put this item at the top of "Open right now".
        item = _pr(9, "vendor audit: rotate the shared credentials", secret,
                   "OPEN", "2026-07-25T04:00:00Z")
        base = main_nodes()
        positions = {"first": [item] + base,
                     "middle": base[:5] + [item] + base[5:],
                     "last": base + [item]}
        for name, nodes in positions.items():
            with self.subTest(position=name):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                before = self.readme.read_bytes()
                MockHandler.requests = []
                self.serve(nodes=nodes)
                code, out = self.run_main()
                self.assertEqual(code, 1, out)
                self.assertEqual(self.readme.read_bytes(), before)
                self.assertIn("::error::", out)
                self.assertIn("private repository", out)
                # The guard fires before ANY further work: npm is never asked, so
                # a private hit cannot cost a request to a third party either.
                self.assertEqual(self.npm_requests(), [])
                # Neither the repository name nor the private title may show up in
                # the log or (the assertion that actually matters) in the file
                # that the next commit would publish.
                published = self.readme.read_text(encoding="utf-8")
                for leak in ("secret-vendor-audit", "rotate the shared credentials",
                             "journaway"):
                    self.assertNotIn(leak, out)
                    self.assertNotIn(leak, published)

    def test_missing_isprivate_field_is_treated_as_private(self):
        """GUARD (c), fail-closed default: no proof of publicness, no render."""
        before = self.readme.read_bytes()
        unknown = {"nameWithOwner": "acme/unknown", "isFork": False,
                   "stargazerCount": 3, "owner": {"login": "acme"}}
        self.serve(nodes=main_nodes() + [_issue(1, "unknown provenance", unknown, "OPEN",
                                                "2026-07-25T04:00:00Z")])
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertIn("private repository", out)

    def test_denylisted_owner_aborts_even_when_public(self):
        """GUARD (d): a denylisted owner aborts even with isPrivate == false;
        the message proves the DENYLIST fired, not the private-repo check."""
        before = self.readme.read_bytes()
        owned = {"nameWithOwner": "journaway/booking-core", "isFork": False, "isPrivate": False,
                 "stargazerCount": 0, "owner": {"login": "journaway"}}
        self.serve(nodes=main_nodes() + [_pr(4, "chore: bump the vendor SDK", owned, "OPEN",
                                             "2026-07-25T04:00:00Z")])
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertIn("denylisted owner", out)
        self.assertNotIn("private repository", out)
        self.assertNotIn("booking-core", out)

    def test_denylisted_owner_in_rendered_block_aborts(self):
        """GUARD (d), output side: case-insensitive, on the finished text."""
        for block in ("\u2026 journaway \u2026", "\u2026 JOURNAWAY \u2026", "[`x/Journaway-Core`](\u2026)"):
            with self.subTest(block=block):
                with self.assertRaises(story.Fatal):
                    story.assert_no_denylisted_owner(block)
        story.assert_no_denylisted_owner(GOLDEN)     # the real block must pass

    def test_no_authorization_header_is_sent_to_npm(self):
        """GUARD (a) continued: the public registry never sees a credential."""
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        gets = self.npm_requests()
        self.assertEqual(len(gets), 2 * len(registry_objects()))
        # The registry search is a third unauthenticated endpoint, same rule.
        self.assertEqual(len(self.registry_requests()), 1)
        for request in gets + self.registry_requests():
            self.assertNotIn("authorization", request["headers"])
            self.assertNotIn("gho_ok", json.dumps(request["headers"]))
        windows = {request["path"].split("/")[3] for request in gets}
        self.assertEqual(windows, {"last-month", "last-year"})

    # ── T-6 ... T-13: fail-closed and idempotency ─────────────────────────────

    def test_idempotent_second_run_makes_no_change(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("README block updated", out)
        first = self.readme.read_bytes()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("no change", out)
        self.assertEqual(self.readme.read_bytes(), first)

    def test_degraded_graphql_leaves_story_untouched_and_marks_fresh(self):
        MockHandler.graphql_responder = staticmethod(
            lambda payload: (503, b'{"message":"upstream unavailable"}'))
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("::error::", out)
        self.assertIn(OLD_STORY, self.readme.read_text(encoding="utf-8"))
        self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
            story.STORY_START, OLD_STORY, story.STORY_END))
        fresh = self.fresh_block()
        self.assertIn("degraded", fresh)
        self.assertIn("the GitHub API was unavailable", fresh)
        self.assertNotIn("Regenerated", fresh)
        # All three attempts were spent before degrading, and npm was never asked.
        self.assertEqual(len(self.graphql_bodies()), len(story.RETRY_BACKOFF) + 1)
        self.assertEqual(self.npm_requests(), [])

    def test_degraded_npm_leaves_story_untouched_and_marks_fresh(self):
        MockHandler.npm_responder = staticmethod(
            lambda window, name: (500, b'{"error":"boom"}'))
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("::error::", out)
        self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
            story.STORY_START, OLD_STORY, story.STORY_END))
        fresh = self.fresh_block()
        self.assertIn("degraded", fresh)
        self.assertIn("api.npmjs.org was unavailable", fresh)
        # No npm figure leaked into the README.
        for figure in ("18,838", "1,075", "16,667"):
            self.assertNotIn(figure, self.readme.read_text(encoding="utf-8"))

    def test_marker_problems_abort(self):
        cases = {
            "story start missing": README_TEMPLATE.replace(story.STORY_START, ""),
            "story end missing": README_TEMPLATE.replace(story.STORY_END, ""),
            "story start duplicated": README_TEMPLATE.replace(
                story.STORY_START, story.STORY_START + "\n" + story.STORY_START),
            "story end duplicated": README_TEMPLATE.replace(
                story.STORY_END, story.STORY_END + "\n" + story.STORY_END),
            "story end before start": README_TEMPLATE.replace(
                "%s\n%s\n%s" % (story.STORY_START, OLD_STORY, story.STORY_END),
                "%s\n%s\n%s" % (story.STORY_END, OLD_STORY, story.STORY_START)),
            "fresh pair missing": README_TEMPLATE.replace(story.FRESH_START, "").replace(
                story.FRESH_END, ""),
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                MockHandler.requests = []
                self.readme.write_text(content, encoding="utf-8")
                before = self.readme.read_bytes()
                code, out = self.run_main()
                self.assertEqual(code, 1)
                self.assertEqual(self.readme.read_bytes(), before)
                self.assertIn("::error::", out)
                # Broken markers are our defect: found before any request.
                self.assertEqual(MockHandler.requests, [])

    def test_missing_token_aborts_before_any_request(self):
        os.environ.pop("GH_TOKEN", None)
        before = self.readme.read_bytes()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertEqual(MockHandler.requests, [])
        self.assertIn("GH_TOKEN", out)

    def test_transient_503_then_200_succeeds_with_retry(self):
        state = {"failed": False}

        def responder(payload):
            if not state["failed"]:
                state["failed"] = True
                return 503, b'{"message":"try again"}'
            return default_graphql(payload)

        MockHandler.graphql_responder = staticmethod(responder)
        os.environ.update(RUN_ENV)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("retry 1/", out)
        # 1 rejected + 1 retried main page + 1 attribution page + 1 repository page.
        self.assertEqual(len(self.graphql_bodies()), 4)
        fresh = self.fresh_block()
        self.assertNotIn("degraded", fresh)
        self.assertIn("Regenerated", fresh)
        # A recovered transient failure yields the FULL block, not a degraded one.
        self.assertIn(GOLDEN, self.readme.read_text(encoding="utf-8"))

    def test_graphql_resource_limit_is_retried_then_succeeds(self):
        state = {"failed": False}

        def responder(payload):
            if not state["failed"]:
                state["failed"] = True
                return 200, json.dumps({"data": {"search": None}, "errors": [
                    {"type": "RESOURCE_LIMITS_EXCEEDED",
                     "message": "Query has exceeded resource limits."}]}).encode()
            return default_graphql(payload)

        MockHandler.graphql_responder = staticmethod(responder)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("resource limits", out)
        self.assertNotIn("degraded", self.fresh_block())

    def test_401_is_not_retried_and_is_fatal(self):
        before = self.readme.read_bytes()
        MockHandler.graphql_responder = staticmethod(
            lambda payload: (401, b'{"message":"Bad credentials"}'))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertEqual(len(self.graphql_bodies()), 1)      # a real error is never retried
        self.assertIn("HTTP 401 from the GitHub API", out)

    def test_npm_404_for_a_discovered_package_degrades(self):
        """The registry lists a package the download counter does not know: the
        two sources disagree, and a sum without that package would be presented
        as complete. Degraded, never a smaller total."""
        def responder(window, name):
            if name == "the-real-unifi-mcp":
                return 404, b'{"error":"package the-real-unifi-mcp not found"}'
            return default_npm(window, name)

        MockHandler.npm_responder = staticmethod(responder)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("::error::", out)
        self.assertIn("the-real-unifi-mcp", out)          # the log names the culprit
        self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
            story.STORY_START, OLD_STORY, story.STORY_END))
        self.assertIn("api.npmjs.org returned an inconsistent snapshot", self.fresh_block())
        # A non-404 4xx on the counter is still OUR defect: fatal, nothing written.
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        before = self.readme.read_bytes()
        MockHandler.npm_responder = staticmethod(lambda window, name: (400, b'{"e":1}'))
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.readme.read_bytes(), before)

    def test_inconsistent_attribution_snapshot_degrades(self):
        """D7: a closed-PR count that does not add up is never rendered."""
        self.serve(attribution=attribution_nodes()[:20])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("::error::", out)
        self.assertIn("inconsistent snapshot", self.fresh_block())
        self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
            story.STORY_START, OLD_STORY, story.STORY_END))

    def test_pagination_follows_the_cursor(self):
        cursors = []

        def responder(payload):
            variables = (payload or {}).get("variables") or {}
            cursors.append(variables.get("after"))
            nodes = {story.SEARCH_ALL: main_nodes(),
                     story.SEARCH_CLOSED: attribution_nodes(),
                     story.SEARCH_REPOS: PUBLIC_REPOS}[variables.get("q")]
            return 200, search_response(nodes, variables.get("after"), page_size=10)

        MockHandler.graphql_responder = staticmethod(responder)
        os.environ.update(RUN_ENV)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        # 43 main nodes, 24 attribution nodes and 5 repositories at 10 per page:
        # every page after the first carries the previous endCursor, and the
        # result is unchanged.
        self.assertEqual(cursors, [None, "10", "20", "30", "40", None, "10", "20", None])
        self.assertIn(GOLDEN, self.readme.read_text(encoding="utf-8"))

    # ── T-14 ... T-24: renderer behaviour ─────────────────────────────────────

    def test_golden_block_from_real_fixture(self):
        os.environ.update(RUN_ENV)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        text = self.readme.read_text(encoding="utf-8")
        self.assertIn(GOLDEN, text)
        # The rest of the document is untouched, including the hero.
        self.assertIn("Hero content above the fold", text)
        self.assertNotIn(OLD_STORY, text)
        self.assertIn("%d items in 10 upstream repos, graphql cost 3, npm 6/6 ok"
                      % (len(main_nodes()) - 3), out)      # minus the three fork items

    def test_fork_items_are_excluded_everywhere(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        text = self.readme.read_text(encoding="utf-8")
        self.assertNotIn("VasyaYovbak", text)
        self.assertIn("Contributions to 10 upstream repositories", text)
        self.assertNotIn("Contributions to 11 upstream repositories", text)
        # The two fork merges must not appear under "Merged upstream", only the
        # two real upstream ones are there.
        merged = text[text.index("**Merged upstream**"):text.index("**Open right now**")]
        self.assertEqual(merged.count("\n  - [#"), 2)
        self.assertIn("2 pull requests", merged)

    def test_open_list_capped_at_three_and_sorted_desc(self):
        items = [
            _issue(11, "oldest", CLAUDE_CODE, "OPEN", "2026-07-01T00:00:00Z"),
            _issue(12, "newest", CLAUDE_CODE, "OPEN", "2026-07-20T00:00:00Z"),
            # Same updatedAt: the tie-break is (nameWithOwner, number) ascending.
            _issue(14, "tie b", HERMES, "OPEN", "2026-07-10T00:00:00Z"),
            _issue(13, "tie a", CLAUDE_CODE, "OPEN", "2026-07-10T00:00:00Z"),
            _issue(15, "middle", OPENCLAW, "OPEN", "2026-07-15T00:00:00Z"),
        ]
        self.serve(nodes=items, attribution=[])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        rendered = [line for line in self.story_block().splitlines()
                    if line.startswith("- [`")]
        self.assertEqual(len(rendered), 3)
        self.assertIn("#12`", rendered[0])
        self.assertIn("#15`", rendered[1])
        # Tie on updatedAt -> nameWithOwner ascending by code point, so
        # "NousResearch/..." precedes "anthropics/...". Arbitrary but fixed.
        self.assertIn("#14`", rendered[2])
        # Feeding the same set in reverse order must produce the same bytes.
        first = self.readme.read_bytes()
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        self.serve(nodes=list(reversed(items)), attribution=[])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.readme.read_bytes(), first)

    def test_dismissed_items_are_skipped(self):
        # Drop the two claude-code items so the duplicate-labelled hermes issue
        # would be rank 2 if it were not filtered out.
        nodes = [n for n in main_nodes() if n["repository"] is not CLAUDE_CODE]
        self.serve(nodes=nodes)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        self.assertNotIn("#64961", block)
        self.assertNotIn("first-class mobile", block)
        self.assertIn("#64962", block)               # the next item moved up
        self.assertIn("#54697", block)

    def test_only_allowlisted_labels_are_rendered(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        for routing in ("area:core", "area:mcp", "area:agent-sdk", "area:cowork",
                        "stale", "P3", "sweeper:", "comp/", "type/feature"):
            self.assertNotIn(routing, block)
        self.assertIn("labelled `bug`, `has repro`, `regression`", block)
        self.assertNotIn("labels:", block)

    def test_star_magnitude_floored_to_thousands(self):
        self.assertEqual(story.kmag(66565), "66k")
        self.assertEqual(story.kmag(1000), "1k")
        self.assertEqual(story.kmag(1999), "1k")
        self.assertEqual(story.kmag(999), "")
        self.assertEqual(story.kmag(0), "")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        self.assertIn("(66k stars)", block)
        self.assertNotIn("66565", block)
        self.assertNotIn("66,565", block)

    def test_attribution_sentence_is_derived_not_asserted(self):
        cases = (
            ("13 bot / 11 self", {}, "13 by the repository's own automation (`github-actions`), "
                                     "11 by me, none by a maintainer."),
            ("one foreign human", {"other": (10310,)},
             "12 by the repository's own automation (`github-actions`), 11 by me, "
             "1 by someone else."),
            ("actor is null", {"unknown": (10310,)},
             "12 by the repository's own automation (`github-actions`), 11 by me, "
             "1 by someone else."),
        )
        for name, kwargs, expected in cases:
            with self.subTest(case=name):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                self.serve(attribution=attribution_nodes(**kwargs))
                code, out = self.run_main()
                self.assertEqual(code, 0, out)
                block = self.story_block()
                self.assertIn("24 of my 25 pull requests are closed: " + expected, block)
                if kwargs:
                    self.assertNotIn("none by a maintainer", block)

    def test_npm_unlinkable_packages_count_but_are_never_named(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        text = self.readme.read_text(encoding="utf-8")
        self.assertIn("**18,838 npm downloads in the last 12 months**", text)
        self.assertIn("across 6 published packages", text)
        self.assertIn("1,075 of them in the last 30 days", text)
        self.assertIn("3 further packages account for the remaining 1,360", text)
        for hidden in ("the-real-snipeit-mcp", "the-real-unifi-mcp", "opencode-abstraction-scanner"):
            self.assertNotIn(hidden, text)
            # ...while their downloads WERE fetched: the sum is honest.
            self.assertTrue(any(hidden in request["path"] for request in self.npm_requests()))
        self.assertEqual(16667 + 433 + 378 + 1360, 18838)

    def test_titles_are_shortened_and_markdown_escaped(self):
        nasty = ("fix: *bold* _under_ [link] <img> | pipe ` tick "
                 + "x" * 200)
        self.serve(nodes=[_issue(7, nasty, CLAUDE_CODE, "OPEN", "2026-07-20T00:00:00Z")],
                   attribution=[])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        text = self.readme.read_text(encoding="utf-8")
        line = [ln for ln in text.splitlines() if ln.startswith("- [`anthropics")][0]
        self.assertIn(r"\*bold\* \_under\_ \[link\] \<img\> \| pipe tick", line)
        self.assertNotIn("` tick", line)                    # the backtick was removed
        self.assertTrue(line.rstrip().endswith("· updated 2026-07-20"))
        self.assertIn("\u2026", line)
        self.assertNotIn("xxx", line)                       # the 200-char tail was cut
        self.assertEqual(text.count(story.STORY_START), 1)
        self.assertEqual(text.count(story.STORY_END), 1)
        # The visible title stays inside the cap; escaping happens afterwards.
        self.assertLessEqual(len(story.shorten(nasty)), story.MAX_TITLE)
        self.assertTrue(story.shorten(nasty).endswith("tick\u2026"))

    def test_empty_sections_are_omitted(self):
        without_merges = [n for n in main_nodes() if not n.get("mergedAt")]
        self.serve(nodes=without_merges)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        self.assertNotIn("**Merged upstream**", block)
        self.assertIn("**Open right now**", block)
        self.assertNotIn("n/a", block)
        self.assertNotIn("\n\n\n", block)                   # no hole where the rubric was

        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        closed_only = [n for n in main_nodes() if n.get("state") != "OPEN"]
        self.serve(nodes=closed_only)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        self.assertNotIn("**Open right now**", block)
        self.assertIn("**Merged upstream**", block)
        self.assertIn("24 of my 24 pull requests are closed", block)

    def test_run_link_omitted_without_github_env(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        fresh = self.fresh_block()
        self.assertNotIn("actions/runs", fresh)
        self.assertNotIn("run #", fresh)
        self.assertIn("Regenerated <strong>2026-07-25 05:32 UTC</strong>", fresh)
        self.assertIn("scheduled daily at 04:30 UTC", fresh)

        for missing in RUN_ENV:
            with self.subTest(missing=missing):
                partial = {k: v for k, v in RUN_ENV.items() if k != missing}
                os.environ.update(partial)
                os.environ.pop(missing, None)
                self.assertIsNone(story.run_info())
                for key in RUN_ENV:
                    os.environ.pop(key, None)

        os.environ.update(RUN_ENV)
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn('<a href="https://github.com/mguttmann/mguttmann/actions/runs/'
                      '30145867690">run #44</a>', self.fresh_block())

    def test_degraded_fresh_variants_are_exact(self):
        run = {"url": "https://github.com/mguttmann/mguttmann/actions/runs/30145867690",
               "number": "44"}
        now = story._parse_now(NOW)
        self.assertEqual(
            story.render_fresh(now, run, "api.npmjs.org was unavailable"),
            '<!-- FRESH:START -->\n<sub>:arrows_counterclockwise: Last generator run '
            '<strong>2026-07-25 05:32 UTC</strong> · '
            '<a href="https://github.com/mguttmann/mguttmann/actions/runs/30145867690">run #44</a>'
            ' · <strong>degraded</strong>: api.npmjs.org was unavailable, the section above is '
            'unchanged from an earlier run · scheduled daily at 04:30 UTC</sub>\n'
            '<!-- FRESH:END -->')
        self.assertIn("the GitHub API was unavailable",
                      story.render_fresh(now, None, "the GitHub API was unavailable"))
        self.assertNotIn("run #", story.render_fresh(now, None, None))

    def test_only_one_write_and_only_on_change(self):
        """FAIL-CLOSED, structurally: exactly one write per run, zero when fatal.

        Counted by intercepting the real write syscalls (see
        count_readme_writes), not by grepping the source: a partial render must
        be UNREACHABLE, and "there is only one write_text in the file" is a
        statement about today's text, not about behaviour."""
        source = Path(story.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("write_text("), 1)      # kept as a cheap tripwire

        # 1. Success: the whole document is committed in a single write.
        (code, out), writes = self.count_readme_writes()
        self.assertEqual(code, 0, out)
        self.assertEqual(writes, ["write_text"], writes)

        # 2. Idempotent second run: no write at all, so the mtime cannot move.
        stamp = self.readme.stat().st_mtime_ns
        (code, out), writes = self.count_readme_writes()
        self.assertEqual(code, 0, out)
        self.assertIn("no change", out)
        self.assertEqual(writes, [], writes)
        self.assertEqual(self.readme.stat().st_mtime_ns, stamp)

        # 3. Fatal (guard violation): ZERO writes, not one write of old content.
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        secret = {"nameWithOwner": "acme/private", "isFork": False, "isPrivate": True,
                  "stargazerCount": 0, "owner": {"login": "acme"}}
        self.serve(nodes=main_nodes() + [_issue(1, "x", secret, "OPEN", "2026-07-25T04:00:00Z")])
        (code, out), writes = self.count_readme_writes()
        self.assertEqual(code, 1, out)
        self.assertEqual(writes, [], writes)

        # 4. Degraded: exactly one write, and it carries the untouched STORY.
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        MockHandler.npm_responder = staticmethod(lambda window, name: (503, b'{"e":1}'))
        self.serve()
        (code, out), writes = self.count_readme_writes()
        self.assertEqual(code, 0, out)
        self.assertEqual(writes, ["write_text"], writes)
        self.assertIn(OLD_STORY, self.readme.read_text(encoding="utf-8"))

    # ── added by the tester ─────────────────────────────────────────────────

    def test_token_never_reaches_the_log_on_any_path(self):
        """The token is the one string that must never be printable, on the happy
        path and on every failure path alike: a traceback or an error message
        that echoes a request is how credentials end up in a public run log."""
        secret_token = "gho_LIVE_TOKEN_MUST_NEVER_BE_LOGGED"
        os.environ["GH_TOKEN"] = secret_token
        secret_repo = {"nameWithOwner": "acme/private", "isFork": False, "isPrivate": True,
                       "stargazerCount": 0, "owner": {"login": "acme"}}
        paths = {
            "success": lambda: None,
            "graphql 503": lambda: setattr(MockHandler, "graphql_responder",
                                           staticmethod(lambda p: (503, b'{"m":"down"}'))),
            "graphql 401": lambda: setattr(MockHandler, "graphql_responder",
                                           staticmethod(lambda p: (401, b'{"m":"bad creds"}'))),
            "malformed json": lambda: setattr(MockHandler, "graphql_responder",
                                              staticmethod(lambda p: (200, b'not json at all'))),
            "npm 500": lambda: setattr(MockHandler, "npm_responder",
                                       staticmethod(lambda w, n: (500, b'{"e":1}'))),
            "npm 404": lambda: setattr(MockHandler, "npm_responder",
                                       staticmethod(lambda w, n: (404, b'{"e":"gone"}'))),
            "guard violation": lambda: self.serve(
                nodes=main_nodes() + [_issue(1, "x", secret_repo, "OPEN",
                                            "2026-07-25T04:00:00Z")]),
        }
        for name, arrange in paths.items():
            with self.subTest(path=name):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                MockHandler.graphql_responder = staticmethod(default_graphql)
                MockHandler.npm_responder = staticmethod(default_npm)
                arrange()
                try:
                    code, out = self.run_main()
                except BaseException as exc:            # an escaping exception is a finding
                    code, out = "raised", "%s: %s" % (type(exc).__name__, exc)
                self.assertNotIn(secret_token, out)
                self.assertNotIn("Bearer", out)
                self.assertNotIn(secret_token, self.readme.read_text(encoding="utf-8"))

    def test_query_documents_never_request_mutable_numbers(self):
        """Contract on the GraphQL documents (hard requirement 5): the numbers
        that drift (line counts and comment counts) must not even be FETCHED.
        A field that is not in the document cannot be rendered by accident later,
        and `comments` provably disagrees with the number GitHub's own UI shows."""
        for name, doc in (("MAIN_QUERY", story.MAIN_QUERY),
                          ("ATTRIBUTION_QUERY", story.ATTRIBUTION_QUERY)):
            for field in ("additions", "deletions", "changedFiles", "commits",
                          "comments", "commentCount", "reactions", "totalCount",
                          "forkCount", "watchers"):
                with self.subTest(document=name, field=field):
                    self.assertNotIn(field, doc)
        # `labels(first: 20)` is load-bearing, not cosmetic: at first: 5 the
        # validating labels of an eight-label item fall off the end silently.
        self.assertIn("labels(first: 20)", story.MAIN_QUERY)

    def test_no_mutable_number_is_rendered(self):
        """Requirement 5 on the OUTPUT side, driven by a payload that offers every
        tempting number: line counts, comment counts and a raw star total."""
        greedy = dict(OPEN_32301,
                      additions=7661, deletions=231, comments=8, changedFiles=142)
        self.serve(nodes=[greedy] + [n for n in main_nodes() if n is not OPEN_32301])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        for mutable in ("7661", "7,661", "+7,661", "231", "7.7k", "142 files",
                        "8 comments", "189513", "189,513", "66565", "66,565"):
            self.assertNotIn(mutable, block)
        # issueCount is fetched for pagination but must never be claimed as a total.
        self.assertNotIn("115", block)
        self.assertNotIn("113 ", block)
        # What replaces the numbers: identity and a floored magnitude class.
        self.assertIn("merged by [`code-yeongyu`]", block)
        self.assertIn("(66k stars)", block)

    def test_midflight_connection_failure_degrades_instead_of_crashing(self):
        """A connection that dies AFTER the request was sent.

        This is not a hypothetical: it happened on the first live smoke run of
        this script against api.npmjs.org (RemoteDisconnected). urllib only wraps
        errors raised by h.request() into URLError; anything raised by
        getresponse() or read() propagates as ConnectionResetError /
        RemoteDisconnected / IncompleteRead, none of which is urllib.error.URLError
        or TimeoutError.

        Required behaviour per the error taxonomy: a failure of the outside world
        is Degraded: retried, then exit 0 with STORY byte-identical and FRESH
        openly marked degraded. An uncaught exception instead skips the write
        entirely, so the public README keeps the PREVIOUS run's "Regenerated ..."
        stamp and ages silently, which is exactly the failure mode the FRESH
        marker exists to prevent."""
        for mode in ("close", "reset", "truncate"):
            with self.subTest(mode=mode):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                before = self.readme.read_bytes()
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(8)
                stop = threading.Event()

                def serve(server=server, stop=stop, mode=mode):
                    while not stop.is_set():
                        try:
                            conn, _ = server.accept()
                        except OSError:
                            return
                        try:
                            conn.recv(65535)
                            if mode == "reset":       # hard RST rather than FIN
                                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                                b"\x01" + b"\x00" * 7)
                            elif mode == "truncate":  # promise 500 bytes, send 9
                                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500"
                                             b"\r\n\r\n{\"downloa")
                            conn.close()
                        except OSError:
                            pass

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                original = story.GITHUB_GRAPHQL
                story.GITHUB_GRAPHQL = "http://127.0.0.1:%d/graphql" % server.getsockname()[1]
                try:
                    try:
                        code, out = self.run_main()
                    except BaseException as exc:
                        self.fail("main() must not raise; %s escaped the error taxonomy "
                                  "(%s). A crash writes nothing, so FRESH keeps the "
                                  "previous run's stamp and the README ages silently."
                                  % (type(exc).__name__, exc))
                finally:
                    story.GITHUB_GRAPHQL = original
                    stop.set()
                    server.close()
                self.assertEqual(code, 0, out)
                self.assertIn("::error::", out)
                self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
                    story.STORY_START, OLD_STORY, story.STORY_END))
                fresh = self.fresh_block()
                self.assertIn("degraded", fresh)
                self.assertIn("the GitHub API was unavailable", fresh)
                self.assertNotEqual(self.readme.read_bytes(), before)

    # ── added by the September ticket (story pipeline completion) ──────────

    def test_transport_error_class_is_retried_then_degrades(self):
        """The CLASS behind the mid-flight crash, not its three instances: every
        transport error urlopen/read can raise (OSError and every
        http.client.HTTPException) is retried and then degrades."""
        import http.client
        import ssl
        import urllib.request as request_module
        for exc in (http.client.RemoteDisconnected("gone"), http.client.IncompleteRead(b"x", 9),
                    http.client.BadStatusLine("junk"), ConnectionResetError(54, "reset"),
                    ConnectionAbortedError(53, "aborted"), BrokenPipeError(32, "pipe"),
                    ssl.SSLError("bad record mac"), OSError(65, "no route")):
            with self.subTest(exc=type(exc).__name__):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                calls = []

                def failing(*a, exc=exc, **k):
                    calls.append(1)
                    raise exc

                original = request_module.urlopen
                request_module.urlopen = failing
                try:
                    code, out = self.run_main()
                finally:
                    request_module.urlopen = original
                self.assertEqual(code, 0, out)
                self.assertEqual(len(calls), len(story.RETRY_BACKOFF) + 1)
                self.assertIn("retry 1/", out)
                self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
                    story.STORY_START, OLD_STORY, story.STORY_END))
                self.assertIn("degraded", self.fresh_block())

    def test_registry_search_discovers_a_new_package_without_code_change(self):
        """A package published tomorrow enters the sum on the next run: the
        package list comes from the registry, not from a table in this repo."""
        objects = registry_objects() + [
            _registry_object("brand-new-mcp", "git+https://github.com/mguttmann/brand-new-mcp.git"),
            _registry_object("hidden-new-mcp", "git+https://github.com/mguttmann/hidden-new-mcp.git")]
        MockHandler.registry_responder = staticmethod(lambda query: (200, registry_response(objects)))
        points = dict(NPM_POINTS, **{"brand-new-mcp": (5, 1000), "hidden-new-mcp": (1, 10)})
        MockHandler.npm_responder = staticmethod(lambda window, name: (200, json.dumps(
            {"downloads": points[name][0 if window == "last-month" else 1]}).encode()))
        self.serve(repos=PUBLIC_REPOS + [_public_repo("brand-new-mcp")])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        block = self.story_block()
        self.assertIn("**19,848 npm downloads in the last 12 months** across 8 published packages", block)
        self.assertIn("[`brand-new-mcp`](https://github.com/mguttmann/brand-new-mcp) 1,000", block)
        self.assertNotIn("hidden-new-mcp", block)
        self.assertIn("4 further packages account for the remaining 1,370", block)
        self.assertIn("npm 8/8 ok", out)
        # The search itself: the maintainer qualifier and the maximum page size.
        query = self.registry_requests()[0]["path"].partition("?")[2]
        self.assertIn("text=maintainer%3Am_guttmann", query)
        self.assertIn("size=250", query)

    def test_package_is_named_only_with_a_provably_public_repository(self):
        cases = {
            # No public repository at all: every package counts, none is named.
            "no public repos": ([], registry_objects(), 0),
            # The declared repository belongs to someone else: never linked
            # under the author's name, even if a same-named public repo exists.
            "foreign owner": (PUBLIC_REPOS, [_registry_object(
                "opencode-sonarqube", "git+https://github.com/someone-else/opencode-sonarqube.git")]
                + registry_objects()[1:], 2),
            # No repository field at all.
            "no repository": (PUBLIC_REPOS, [_registry_object("opencode-sonarqube", None)]
                              + registry_objects()[1:], 2),
        }
        for name, (repos, objects, named) in cases.items():
            with self.subTest(case=name):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                MockHandler.registry_responder = staticmethod(
                    lambda query, objects=objects: (200, registry_response(objects)))
                self.serve(repos=repos)
                code, out = self.run_main()
                self.assertEqual(code, 0, out)
                block = self.story_block()
                self.assertIn("**18,838 npm downloads", block)          # the sum never shrinks
                self.assertEqual(block.count("](https://github.com/mguttmann/"), named, block)
                self.assertIn("%d further packages" % (6 - named), block)
                if name != "no public repos":
                    self.assertNotIn("[`opencode-sonarqube`]", block)
                # Only the naming rule is stated, never an unproven reason.
                self.assertIn("only packages with a verified public repository are named", block)
                self.assertNotIn("not public", block)

    def test_repository_search_result_is_guarded_like_the_item_search(self):
        """GUARD (c) on the repository search: isPrivate must be explicitly
        false, a missing field is treated as private, nothing is written."""
        for flag in (True, None):
            with self.subTest(isPrivate=flag):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                before = self.readme.read_bytes()
                repo = {"name": "the-real-unifi-mcp", "owner": {"login": "mguttmann"}}
                if flag is not None:
                    repo["isPrivate"] = flag
                self.serve(repos=PUBLIC_REPOS + [repo])
                code, out = self.run_main()
                self.assertEqual(code, 1, out)
                self.assertEqual(self.readme.read_bytes(), before)
                self.assertIn("private repository", out)
                self.assertEqual(self.npm_requests(), [])

    def test_registry_search_failure_degrades_and_never_renders_a_partial_sum(self):
        cases = {
            "503": lambda query: (503, b'{"error":"down"}'),
            "400": lambda query: (400, b'{"error":"bad"}'),
            "malformed json": lambda query: (200, b"<html>not json</html>"),
            "total larger than the page": lambda query: (
                200, registry_response(registry_objects(), total=251)),
            "truncated page": lambda query: (
                200, registry_response(registry_objects()[:4], total=6)),
            "total missing": lambda query: (200, json.dumps(
                {"objects": registry_objects()}).encode()),
            "bad package name": lambda query: (200, registry_response(
                registry_objects() + [_registry_object("Bad Name](x)", None)])),
            "nothing by the maintainer": lambda query: (200, registry_response(
                [_registry_object("foreign", None, maintainers=("someone",))])),
        }
        for name, responder in cases.items():
            with self.subTest(case=name):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                MockHandler.requests = []
                MockHandler.registry_responder = staticmethod(responder)
                code, out = self.run_main()
                self.assertEqual(code, 0, out)
                self.assertIn("::error::", out)
                self.assertEqual(self.story_block(), "%s\n%s\n%s" % (
                    story.STORY_START, OLD_STORY, story.STORY_END))
                fresh = self.fresh_block()
                self.assertIn("degraded", fresh)
                self.assertIn("the npm registry search", fresh)
                # Not a single download figure was even fetched.
                self.assertEqual(self.npm_requests(), [])

    def test_foreign_maintainer_packages_are_not_counted(self):
        objects = registry_objects() + [
            _registry_object("someone-elses-pkg", None, maintainers=("someone",))]
        MockHandler.registry_responder = staticmethod(lambda query: (200, registry_response(objects)))
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("across 6 published packages", self.story_block())
        self.assertFalse(any("someone-elses-pkg" in r["path"] for r in self.npm_requests()))

    def _velaterm(self, count, merged_by="vlinx-io"):
        repo = {"nameWithOwner": "vlinx-io/VelaTerm", "isFork": False, "isPrivate": False,
                "stargazerCount": 157, "owner": {"login": "vlinx-io"}}
        # mergedAt is synthetic but monotonic in the PR number, and deliberately
        # NOT sorted in the list, so "newest" must come from the sort.
        prs = [_pr(20 + i, "feat(web): change number %d" % i, repo, "MERGED",
                   "2026-09-%02dT10:00:00Z" % (1 + i), merged_at="2026-09-%02dT10:00:00Z" % (1 + i),
                   merged_by=merged_by) for i in range(count)]
        return prs[::2] + prs[1::2]

    def test_merged_block_shows_count_newest_three_and_full_list(self):
        """Point 3 of the brief: 11 merges render as ONE header with the count,
        the three newest titles and a link to the full list, and the 12th merge
        does not make the block one line longer."""
        lengths = []
        for count in (11, 12):
            with self.subTest(count=count):
                self.readme.write_text(README_TEMPLATE, encoding="utf-8")
                self.serve(nodes=main_nodes() + self._velaterm(count))
                code, out = self.run_main()
                self.assertEqual(code, 0, out)
                block = self.story_block()
                lengths.append(len(block.splitlines()))
                merged = block[block.index("**Merged upstream**"):block.index("**Open right now**")]
                vela = merged[merged.index("[`vlinx-io/VelaTerm`]"):]
                vela = vela[:vela.index("\n- ") if "\n- " in vela else len(vela)]
                self.assertIn("%d pull requests merged by [`vlinx-io`](https://github.com/vlinx-io) "
                              "(the repository owner), newest first:" % count, vela)
                titles = [ln for ln in vela.splitlines() if ln.startswith("  - [#")]
                self.assertEqual([t.split("]")[0] for t in titles],
                                 ["  - [#%d" % (20 + count - 1 - k) for k in range(3)])
                self.assertIn("  - [full list](https://github.com/vlinx-io/VelaTerm/pulls?"
                              "q=is%3Apr+is%3Amerged+author%3Amguttmann)", vela)
        self.assertEqual(lengths[0], lengths[1])

    def test_merges_by_the_author_or_an_unknown_merger_do_not_count(self):
        self_merged = self._velaterm(2, merged_by="mguttmann")
        unknown = self._velaterm(1, merged_by=None)
        for i, pr in enumerate(unknown):
            unknown[i] = dict(pr, number=99)
        self.serve(nodes=main_nodes() + self_merged + unknown)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        merged = self.story_block()
        merged = merged[merged.index("**Merged upstream**"):merged.index("**Open right now**")]
        self.assertNotIn("VelaTerm", merged)
        self.assertIn("oh-my-openagent", merged)

    def test_forbidden_dashes_are_normalised_and_never_published(self):
        """The owner's text rule on the OUTPUT: foreign titles with em dashes,
        en dashes and look-alikes are normalised, and nothing the renderer
        writes into the README carries either character."""
        em, en = "\u2014", "\u2013"
        title = "fix: a %s b %s c \u2012 d \u2015 e \ufe58 f" % (em, en)
        self.serve(nodes=main_nodes() + [
            _issue(8, title, CLAUDE_CODE, "OPEN", "2026-07-25T04:00:00Z")])
        os.environ.update(RUN_ENV)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        text = self.readme.read_text(encoding="utf-8")
        self.assertIn("fix: a - b - c - d - e - f", text)
        for block in (self.story_block(), self.fresh_block(), out):
            self.assertNotIn(em, block)
            self.assertNotIn(en, block)
        # The golden fixture itself carries real foreign em dashes (the closed
        # opencode titles and #77704); none may survive into the block.
        self.assertTrue(any(em in n["title"] for n in main_nodes()))
        # Degraded FRESH variant too.
        self.readme.write_text(README_TEMPLATE, encoding="utf-8")
        MockHandler.npm_responder = staticmethod(lambda window, name: (503, b"{}"))
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        for block in (self.fresh_block(), out):
            self.assertNotIn(em, block)
            self.assertNotIn(en, block)
        # The output-side chokepoint fires on anything that slipped through.
        for dash in (em, en):
            with self.assertRaises(story.Fatal):
                story.assert_no_forbidden_dashes("x %s y" % dash)
        story.assert_no_forbidden_dashes(GOLDEN)

    def test_marker_strings_in_foreign_titles_are_neutralised(self):
        hostile = "x <!-- STORY:END --> <!-- FRESH:START --> [y](javascript:z) <b>w</b>"
        self.serve(nodes=[_issue(9, hostile, CLAUDE_CODE, "OPEN", "2026-07-25T04:00:00Z")],
                   attribution=[])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        text = self.readme.read_text(encoding="utf-8")
        for marker in (story.STORY_START, story.STORY_END, story.FRESH_START, story.FRESH_END):
            self.assertEqual(text.count(marker), 1, marker)
        self.assertNotIn("<b>", text)
        self.assertNotIn("[y](", text)
        code, out = self.run_main()                     # and the next run still parses
        self.assertEqual(code, 0, out)
        self.assertIn("no change", out)

    def test_ticket_files_carry_no_forbidden_dashes(self):
        """Tripwire for the owner's text rule on the files this pipeline owns."""
        root = Path(__file__).resolve().parents[3]
        for rel in (".github/scripts/upstream_story.py",
                    ".github/scripts/tests/test_upstream_story.py",
                    ".github/workflows/opencode-pr.yml"):
            with self.subTest(file=rel):
                content = (root / rel).read_text(encoding="utf-8")
                self.assertNotIn("\u2014", content)
                self.assertNotIn("\u2013", content)


if __name__ == "__main__":
    unittest.main()
