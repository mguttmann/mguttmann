#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# graphql_split_proxy.py  ·  Loopback GraphQL split proxy for the compose-3d job
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: GitHub's GraphQL API answers the combined fetchFirst query of
# the pinned yoshi389111/github-profile-3d-contrib@v0.9.2 Action for THIS user
# with {"data":{"user":null},"errors":[{"type":"RESOURCE_LIMITS_EXCEEDED",...}]}
# (deterministic, user-data-dependent query cost). The Action ignores the
# `errors` array and crashes with a TypeError on `user.repositories`. This
# proxy sits on loopback between the Action and api.github.com and splits that
# ONE oversized query into sub-queries that each stay under the limit:
#   - 1x contributionCalendar alone (calendar + repositories combined turned
#     out to be FLAKY upstream — each alone is deterministically OK)
#   - 1x repositories alone
#   - 5x ONE total* contribution field each (two or more in one query FAIL)
# It then reassembles the answers into the exact response shape the Action
# expects (its ResponseType) and STUBS commitContributionsByRepository=[] —
# that field fails upstream in EVERY variant, and the only thing the Action
# derives from it is the per-language panel, which the very next workflow step
# strips out of the SVG anyway (the real language donut comes from Job A).
# With the stub the Action renders a single "other" segment, which the
# language-agnostic strip signature matches and removes.
#
# TOKEN HANDLING (security-critical):
#   - The Authorization header is forwarded EXCLUSIVELY to UPSTREAM
#     (api.github.com/graphql) and to nothing else.
#   - Headers are NEVER logged. The log contains only method, path, query
#     classification, sub-query name, upstream HTTP status and — on failure —
#     the upstream GraphQL error body (which never contains tokens).
#
# SSRF: UPSTREAM is HARDCODED below and never derived from the request or from
# the environment. The server binds to 127.0.0.1 ONLY.
#
# FAIL-LOUD: if any sub-query returns errors or user=null, the proxy replies
# with {"errors":[...]} WITHOUT a `data` key. The Action then throws
# Error(errors[0].message) instead of the meaningless TypeError, the job goes
# red, and the real GraphQL message is visible in the log. No silent
# degradation — every sub-query must succeed for a green run.
#
# All other queries (the Action's fetchNext pagination query, etc.) are passed
# through to UPSTREAM byte-verbatim in both directions.
#
# Standard library only (consistency with lang_card.py / merge_donut_into_3d.py).
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://api.github.com/graphql"  # HARDCODED — never derived from the request (no SSRF)
BIND_HOST = "127.0.0.1"  # loopback ONLY — never exposed
PORT = int(os.environ.get("GRAPHQL_SPLIT_PROXY_PORT", "8877"))
UPSTREAM_TIMEOUT = 30  # seconds per sub-query; fail loud on expiry
TOTAL_FIELDS = (
    "totalCommitContributions",
    "totalIssueContributions",
    "totalPullRequestContributions",
    "totalPullRequestReviewContributions",
    "totalRepositoryContributions",
)
# Captures the optional (from: ..., to: ...) argument list so a YEAR-scoped
# original query propagates its window into every sub-query.
COLLECTION_ARGS_RE = re.compile(r"contributionsCollection\s*(\([^)]*\))?")

CALENDAR_SUBQUERY = (
    "query($login: String!) { user(login: $login) { "
    "contributionsCollection%s { contributionCalendar { isHalloween "
    "totalContributions weeks { contributionDays { contributionCount "
    "contributionLevel date } } } } } }"
)
REPOSITORIES_SUBQUERY = (
    "query($login: String!) { user(login: $login) { "
    "repositories(first: 100, ownerAffiliations: OWNER) { "
    "edges { cursor } nodes { forkCount stargazerCount } } } }"
)
TOTAL_SUBQUERY = (
    "query($login: String!) { user(login: $login) { "
    "contributionsCollection%s { %s } } }"
)


def log(msg: str) -> None:
    # stderr lands in the workflow's log file. NEVER pass header values here.
    sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    sys.stderr.flush()


def is_fetch_first(query: str) -> bool:
    # Whitespace-independent detection of the Action's fetchFirst query; the
    # fetchNext pagination query contains neither substring. Verified against
    # the pinned v0.9.2 sources — the pin guarantees the strings cannot drift.
    return "commitContributionsByRepository" in query and "contributionCalendar" in query


def extract_collection_args(query: str) -> str:
    m = COLLECTION_ARGS_RE.search(query)
    if m and m.group(1):
        return m.group(1)
    return ""


def build_subqueries(args: str) -> list[tuple[str, str]]:
    # Calendar and repositories are queried SEPARATELY: the combined form is
    # flaky upstream (intermittent RESOURCE_LIMITS_EXCEEDED on calendar.weeks),
    # while each alone is deterministically fine. 7 upstream calls total.
    subqueries = [
        ("calendar", CALENDAR_SUBQUERY % args),
        ("repositories", REPOSITORIES_SUBQUERY),
    ]
    # NEVER two total* fields in one query — that is exactly the pattern that
    # trips RESOURCE_LIMITS_EXCEEDED for this user.
    for field in TOTAL_FIELDS:
        subqueries.append((field, TOTAL_SUBQUERY % (args, field)))
    return subqueries


def post_upstream(body: bytes, auth: str | None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if auth:
        # Forwarded to the hardcoded UPSTREAM only — never logged.
        headers["Authorization"] = auth
    req = urllib.request.Request(UPSTREAM, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # GraphQL errors sometimes arrive with a non-200 status; hand the body
        # back to the caller instead of treating it as a transport failure.
        return exc.code, exc.read()


def subresponse_error(name: str, status: int, parsed: dict | None, raw: bytes) -> list | None:
    """Return the errors array to reply with if the sub-response is bad, else None.

    Upstream `errors` entries are passed through verbatim; a synthetic entry is
    built only when upstream provided none (e.g. user=null without errors).
    """
    upstream_errors = None
    if isinstance(parsed, dict):
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors:
            upstream_errors = errors
    data = parsed.get("data") if isinstance(parsed, dict) else None
    user = data.get("user") if isinstance(data, dict) else None
    if status == 200 and parsed is not None and upstream_errors is None and user is not None:
        return None
    # Token-free by construction: the upstream GraphQL body never echoes the
    # Authorization header.
    log("sub-query '%s' failed (HTTP %d): %s" % (name, status, raw.decode("utf-8", "replace")))
    if upstream_errors is not None:
        return upstream_errors
    message = ("graphql_split_proxy: sub-query '%s' returned user=null without errors (HTTP %d)"
               % (name, status))
    # Non-GraphQL error bodies (e.g. secondary rate limit) carry a top-level
    # REST-style "message" — surface it for diagnosis (never contains tokens).
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        message += ": " + parsed["message"]
    return [{"message": message}]


def handle_split(payload: dict, auth: str | None) -> tuple[int, dict]:
    args = extract_collection_args(payload["query"])
    variables = payload.get("variables", {})  # passed through verbatim (carries `login`)
    responses: dict[str, dict] = {}
    for name, subquery in build_subqueries(args):
        body = json.dumps({"query": subquery, "variables": variables}).encode("utf-8")
        status, raw = post_upstream(body, auth)
        log("sub-query '%s' -> upstream HTTP %d" % (name, status))
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        errors = subresponse_error(name, status, parsed, raw)
        if errors is not None:
            # FAIL-LOUD: no `data` key, so the Action throws the GraphQL
            # message instead of crashing with a TypeError on data.user.
            return 200, {"errors": errors}
        responses[name] = parsed
    # Reassemble the exact ResponseType shape the Action expects.
    coll = responses["calendar"]["data"]["user"]["contributionsCollection"]
    coll["commitContributionsByRepository"] = []  # STUB — panel is stripped downstream
    for field in TOTAL_FIELDS:
        coll[field] = responses[field]["data"]["user"]["contributionsCollection"][field]
    result = {"data": {"user": {
        "contributionsCollection": coll,
        "repositories": responses["repositories"]["data"]["user"]["repositories"],
    }}}
    return 200, result


def handle_passthrough(raw_body: bytes, auth: str | None) -> tuple[int, bytes]:
    # fetchNext and anything else: byte-verbatim in both directions.
    return post_upstream(raw_body, auth)


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format: str, *args) -> None:  # noqa: A002 (stdlib signature)
        # Route the default per-request line through log(); it contains only
        # method/path/status — never headers.
        log("%s - %s" % (self.address_string(), format % args))

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_json(self, status: int, obj: dict) -> None:
        self._reply(status, json.dumps(obj).encode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self._reply_json(404, {"errors": [{"message": "graphql_split_proxy: not found"}]})

    def do_POST(self) -> None:
        if self.path != "/graphql":
            self._reply_json(404, {"errors": [{"message": "graphql_split_proxy: not found"}]})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length)
        auth = self.headers.get("Authorization")  # forwarded to UPSTREAM only, never logged
        try:
            payload = json.loads(raw_body)
        except ValueError:
            payload = None
        query = payload.get("query") if isinstance(payload, dict) else None
        if not isinstance(query, str):
            self._reply_json(400, {"errors": [{"message": "graphql_split_proxy: malformed request body"}]})
            return
        try:
            if is_fetch_first(query):
                log("POST /graphql: fetchFirst detected -> splitting")
                status, obj = handle_split(payload, auth)
                self._reply_json(status, obj)
            else:
                log("POST /graphql: passthrough")
                status, body = handle_passthrough(raw_body, auth)
                self._reply(status, body)
        except Exception as exc:  # e.g. URLError / timeout — fail loud, token-free
            log("upstream request failed: %r" % (exc,))
            self._reply_json(502, {"errors": [{
                "message": "graphql_split_proxy: upstream request failed: %r" % (exc,),
            }]})


def create_server(port: int) -> ThreadingHTTPServer:
    # ThreadingHTTPServer so /healthz never blocks behind a running split.
    return ThreadingHTTPServer((BIND_HOST, port), Handler)


def main() -> int:
    srv = create_server(PORT)
    log("listening on http://%s:%d/graphql -> %s" % (BIND_HOST, PORT, UPSTREAM))
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
