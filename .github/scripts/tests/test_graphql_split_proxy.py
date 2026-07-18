#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Unit tests for graphql_split_proxy.py — stdlib unittest only.
#
# Spins up an in-process mock upstream (ThreadingHTTPServer on an ephemeral
# port), patches proxy.UPSTREAM to point at it, starts the proxy itself on an
# ephemeral port, and exercises it over real HTTP with urllib. The fetchFirst
# fixture is the EXACT collapsed query string the pinned 3D Action v0.9.2
# sends (including the surrounding spaces and the spaces after colons).
#
# Run:  python3 .github/scripts/tests/test_graphql_split_proxy.py
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import graphql_split_proxy as proxy  # noqa: E402

# The exact whitespace-collapsed fetchFirst string sent by the pinned Action.
FETCH_FIRST = (
    " query($login: String!) { user(login: $login) { contributionsCollection "
    "{ contributionCalendar { isHalloween totalContributions weeks { "
    "contributionDays { contributionCount contributionLevel date } } } "
    "commitContributionsByRepository(maxRepositories: 100) { repository { "
    "primaryLanguage { name color } } contributions { totalCount } } "
    "totalCommitContributions totalIssueContributions "
    "totalPullRequestContributions totalPullRequestReviewContributions "
    "totalRepositoryContributions } repositories(first: 100, "
    "ownerAffiliations: OWNER) { edges { cursor } nodes { forkCount "
    "stargazerCount } } } } "
)

FETCH_NEXT = (
    " query($login: String!, $cursor: String!) { user(login: $login) { "
    "repositories(after: $cursor, first: 100, ownerAffiliations: OWNER) { "
    "edges { cursor } nodes { forkCount stargazerCount } } } } "
)

TOTAL_VALUES = {
    "totalCommitContributions": 3957,
    "totalIssueContributions": 41,
    "totalPullRequestContributions": 152,
    "totalPullRequestReviewContributions": 12,
    "totalRepositoryContributions": 37,
}

CALENDAR_DATA = {
    "isHalloween": False,
    "totalContributions": 3957,
    "weeks": [{"contributionDays": [
        {"contributionCount": 5, "contributionLevel": "SECOND_QUARTILE", "date": "2026-07-01"},
    ]}],
}

REPOSITORIES_DATA = {
    "edges": [{"cursor": "cursor-1"}],
    "nodes": [{"forkCount": 1, "stargazerCount": 4}],
}


def default_responder(payload, raw_body):
    """Default mock-upstream routing. Returns (status, body_bytes)."""
    query = payload.get("query", "") if isinstance(payload, dict) else ""
    if "contributionCalendar" in query:
        return 200, json.dumps({"data": {"user": {
            "contributionsCollection": {"contributionCalendar": dict(CALENDAR_DATA)},
        }}}).encode()
    for field, value in TOTAL_VALUES.items():
        if field in query:
            return 200, json.dumps({"data": {"user": {
                "contributionsCollection": {field: value},
            }}}).encode()
    return 200, json.dumps({"data": {"user": {"repositories": dict(REPOSITORIES_DATA)}}}).encode()


class MockUpstreamHandler(BaseHTTPRequestHandler):
    requests: list = []          # (query, variables, auth, raw_body)
    responder = staticmethod(default_responder)

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body)
        except ValueError:
            payload = None
        MockUpstreamHandler.requests.append((
            payload.get("query") if isinstance(payload, dict) else None,
            payload.get("variables") if isinstance(payload, dict) else None,
            self.headers.get("Authorization"),
            raw_body,
        ))
        status, body = MockUpstreamHandler.responder(payload, raw_body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Capture proxy log lines instead of stderr (token-hygiene assertions).
        cls.log_lines = []
        cls._orig_log = proxy.log
        proxy.log = lambda msg: cls.log_lines.append(msg)

        cls.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        cls._orig_upstream = proxy.UPSTREAM
        proxy.UPSTREAM = "http://127.0.0.1:%d/graphql" % cls.mock.server_address[1]

        cls.proxy_srv = proxy.create_server(0)
        threading.Thread(target=cls.proxy_srv.serve_forever, daemon=True).start()
        cls.proxy_port = cls.proxy_srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.proxy_srv.shutdown()
        cls.mock.shutdown()
        proxy.UPSTREAM = cls._orig_upstream
        proxy.log = cls._orig_log

    def setUp(self):
        MockUpstreamHandler.requests = []
        MockUpstreamHandler.responder = staticmethod(default_responder)
        type(self).log_lines[:] = []

    # ── helpers ──────────────────────────────────────────────────────────

    def post(self, body, path="/graphql", auth=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.proxy_port, path),
            data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def post_graphql(self, query, variables=None, auth=None):
        body = json.dumps({"query": query,
                           "variables": variables or {"login": "mguttmann"}}).encode()
        status, raw = self.post(body, auth=auth)
        return status, json.loads(raw)

    # ── 1. split correctness ─────────────────────────────────────────────

    def test_split_correctness(self):
        status, _ = self.post_graphql(FETCH_FIRST)
        self.assertEqual(status, 200)
        reqs = MockUpstreamHandler.requests
        self.assertEqual(len(reqs), 7)
        total_re = re.compile(r"total\w+Contributions")
        calendar_reqs = 0
        repo_reqs = 0
        for query, variables, _auth, _raw in reqs:
            self.assertNotIn("commitContributionsByRepository", query)
            self.assertLessEqual(len(set(total_re.findall(query))), 1,
                                 "sub-query carries >=2 total* fields: %s" % query)
            self.assertEqual(variables, {"login": "mguttmann"})
            if "contributionCalendar" in query:
                calendar_reqs += 1
                self.assertIn("isHalloween", query)
                # Calendar and repositories must be SEPARATE sub-queries (the
                # combined form is flaky upstream).
                self.assertNotIn("repositories(first: 100", query)
            if "repositories(first: 100" in query:
                repo_reqs += 1
                self.assertNotIn("contributionsCollection", query)
        self.assertEqual(calendar_reqs, 1)
        self.assertEqual(repo_reqs, 1)
        # Each of the five total* fields was requested exactly once.
        for field in TOTAL_VALUES:
            hits = [q for q, _v, _a, _r in reqs if field in q]
            self.assertEqual(len(hits), 1, field)

    # ── 2. assembly shape ────────────────────────────────────────────────

    def test_assembly_shape(self):
        status, data = self.post_graphql(FETCH_FIRST)
        self.assertEqual(status, 200)
        self.assertNotIn("errors", data)
        user = data["data"]["user"]
        coll = user["contributionsCollection"]
        self.assertEqual(coll["contributionCalendar"], CALENDAR_DATA)
        self.assertEqual(coll["commitContributionsByRepository"], [])
        for field, value in TOTAL_VALUES.items():
            self.assertEqual(coll[field], value)
        self.assertEqual(user["repositories"], REPOSITORIES_DATA)

    # ── 3. contributionsCollection args propagation ──────────────────────

    def test_collection_args_propagation(self):
        args = '(from: "2025-01-01T00:00:00.000Z", to: "2025-12-31T23:59:59.000Z")'
        query = FETCH_FIRST.replace("contributionsCollection ",
                                    "contributionsCollection%s " % args, 1)
        status, data = self.post_graphql(query)
        self.assertEqual(status, 200)
        self.assertNotIn("errors", data)
        self.assertEqual(len(MockUpstreamHandler.requests), 7)
        for q, _v, _a, _r in MockUpstreamHandler.requests:
            if "contributionsCollection" in q:
                self.assertIn("contributionsCollection%s" % args, q)
        with_args = [q for q, _v, _a, _r in MockUpstreamHandler.requests
                     if "contributionsCollection%s" % args in q]
        self.assertEqual(len(with_args), 6)  # all but the repositories sub-query

    # ── 4. passthrough (fetchNext) ───────────────────────────────────────

    def test_passthrough_byte_verbatim(self):
        body = json.dumps({"query": FETCH_NEXT,
                           "variables": {"login": "mguttmann", "cursor": "cursor-1"}}).encode()
        canned = b'{"data":{"user":{"repositories":{"edges":[],"nodes":[]}}}}'
        MockUpstreamHandler.responder = staticmethod(lambda p, r: (200, canned))
        status, raw = self.post(body)
        self.assertEqual(status, 200)
        self.assertEqual(raw, canned)
        self.assertEqual(len(MockUpstreamHandler.requests), 1)
        self.assertEqual(MockUpstreamHandler.requests[0][3], body)

    def test_passthrough_forwards_auth_and_relays_non200_status(self):
        # The passthrough contract is status- AND byte-verbatim in both
        # directions, including upstream failures (e.g. a REST-style 403),
        # and must forward the Authorization header — but never log it.
        token = "bearer TESTSECRET123"
        body = json.dumps({"query": FETCH_NEXT,
                           "variables": {"login": "mguttmann", "cursor": "cursor-1"}}).encode()
        canned = b'{"message":"API rate limit exceeded for installation ID 1."}'
        MockUpstreamHandler.responder = staticmethod(lambda p, r: (403, canned))
        status, raw = self.post(body, auth=token)
        self.assertEqual(status, 403)
        self.assertEqual(raw, canned)
        self.assertEqual(len(MockUpstreamHandler.requests), 1)
        self.assertEqual(MockUpstreamHandler.requests[0][2], token)
        self.assertNotIn("TESTSECRET123", "\n".join(type(self).log_lines))

    # ── 5. error path: RESOURCE_LIMITS_EXCEEDED on a sub-query ──────────

    def test_resource_limits_error_aborts_without_data_key(self):
        upstream_error = {"type": "RESOURCE_LIMITS_EXCEEDED",
                          "message": "Query has exceeded resource limits."}

        def responder(payload, raw_body):
            query = payload.get("query", "")
            if "totalIssueContributions" in query:
                return 200, json.dumps({"data": {"user": None},
                                        "errors": [upstream_error]}).encode()
            return default_responder(payload, raw_body)

        MockUpstreamHandler.responder = staticmethod(responder)
        status, data = self.post_graphql(FETCH_FIRST)
        self.assertEqual(status, 200)
        self.assertNotIn("data", data)
        self.assertEqual(data["errors"], [upstream_error])
        # Sub-query order: calendar, repositories, then TOTAL_FIELDS in order;
        # the failure is the 2nd total* field => exactly 4 upstream requests,
        # then abort.
        self.assertEqual(len(MockUpstreamHandler.requests), 4)

    # ── 6. user:null without errors -> synthetic error ──────────────────

    def test_user_null_without_errors_synthetic(self):
        def responder(payload, raw_body):
            query = payload.get("query", "")
            if "totalRepositoryContributions" in query:
                return 200, json.dumps({"data": {"user": None}}).encode()
            return default_responder(payload, raw_body)

        MockUpstreamHandler.responder = staticmethod(responder)
        status, data = self.post_graphql(FETCH_FIRST)
        self.assertEqual(status, 200)
        self.assertNotIn("data", data)
        self.assertEqual(len(data["errors"]), 1)
        self.assertIn("totalRepositoryContributions", data["errors"][0]["message"])
        self.assertIn("user=null without errors", data["errors"][0]["message"])

    # ── 7. upstream unreachable -> 502 ───────────────────────────────────

    def test_upstream_unreachable(self):
        # Find a port that is definitely closed.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        saved = proxy.UPSTREAM
        proxy.UPSTREAM = "http://127.0.0.1:%d/graphql" % closed_port
        try:
            # Auth is set so the 502 path (log + error message built from
            # repr(exc)) is exercised WITH a token in play — see hygiene
            # asserts below.
            status, data = self.post_graphql(FETCH_FIRST, auth="bearer TESTSECRET123")
        finally:
            proxy.UPSTREAM = saved
        self.assertEqual(status, 502)
        self.assertIn("upstream request failed", data["errors"][0]["message"])
        # Neither the 502 error body nor the log may leak the token.
        self.assertNotIn("TESTSECRET123", json.dumps(data))
        self.assertNotIn("TESTSECRET123", "\n".join(type(self).log_lines))

    # ── 8. malformed bodies, healthz, unknown paths ──────────────────────

    def test_malformed_body_not_json(self):
        status, raw = self.post(b"this is not json")
        self.assertEqual(status, 400)
        self.assertIn("malformed request body", json.loads(raw)["errors"][0]["message"])
        self.assertEqual(MockUpstreamHandler.requests, [])

    def test_malformed_body_missing_query(self):
        status, raw = self.post(json.dumps({"variables": {"login": "x"}}).encode())
        self.assertEqual(status, 400)
        self.assertIn("malformed request body", json.loads(raw)["errors"][0]["message"])

    def test_healthz(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/healthz" % self.proxy_port, timeout=10) as resp:
            self.assertEqual(resp.status, 200)

    def test_post_unknown_path_404(self):
        status, _raw = self.post(b"{}", path="/other")
        self.assertEqual(status, 404)
        self.assertEqual(MockUpstreamHandler.requests, [])

    # ── 9. token hygiene ─────────────────────────────────────────────────

    def test_token_never_logged_but_forwarded(self):
        token = "bearer TESTSECRET123"
        status, _data = self.post_graphql(FETCH_FIRST, auth=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(MockUpstreamHandler.requests), 7)
        for _q, _v, auth, _r in MockUpstreamHandler.requests:
            self.assertEqual(auth, token)
        joined = "\n".join(type(self).log_lines)
        self.assertNotIn("TESTSECRET123", joined)
        self.assertNotIn("bearer", joined.lower())

    def test_token_never_logged_on_sub_query_failure(self):
        # subresponse_error() logs the FULL upstream body on failure — the
        # riskiest logging path. Run it with a token in play and assert the
        # log stays token-free while remaining diagnosable (sub-query name
        # and upstream message must appear).
        token = "bearer TESTSECRET123"

        def responder(payload, raw_body):
            query = payload.get("query", "")
            if "totalIssueContributions" in query:
                return 200, json.dumps({"data": {"user": None}, "errors": [
                    {"type": "RESOURCE_LIMITS_EXCEEDED",
                     "message": "Query has exceeded resource limits."},
                ]}).encode()
            return default_responder(payload, raw_body)

        MockUpstreamHandler.responder = staticmethod(responder)
        status, data = self.post_graphql(FETCH_FIRST, auth=token)
        self.assertEqual(status, 200)
        self.assertNotIn("data", data)
        joined = "\n".join(type(self).log_lines)
        self.assertNotIn("TESTSECRET123", joined)
        self.assertIn("sub-query 'totalIssueContributions' failed", joined)
        self.assertIn("Query has exceeded resource limits.", joined)


if __name__ == "__main__":
    unittest.main()
