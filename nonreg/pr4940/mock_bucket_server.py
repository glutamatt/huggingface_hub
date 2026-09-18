"""A mock of the Hub's bucket `/batch` endpoint, for scenarios production won't produce on demand.

The scenario is selected by the BUCKET NAME, so the same server serves both the SDK harness and
real `hf buckets ...` CLI invocations without any extra plumbing:

    hf buckets rm user/partial200/a.txt -y   ->  serves the `partial200` scenario

The `success` scenario replies with the exact payload the production Hub was observed to send
(`{"success":true,"processed":N,"succeeded":N,"failed":[]}`), so the baseline is not invented.
"""

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


# Each scenario -> (status, body). `body` may be a callable taking the number of operations sent.
SCENARIOS = {
    # --- must NOT raise ---------------------------------------------------------------
    # Exact shape observed from the production Hub on a fully successful batch.
    "success": (200, lambda n: {"success": True, "processed": n, "succeeded": n, "failed": []}),
    # Body the endpoint is not documented to send; must fall through untouched.
    "empty200": (200, lambda n: {}),
    "nofields": (200, lambda n: {"success": True}),
    "jsonlist": (200, lambda n: ["not", "an", "object"]),
    "jsonstring": (200, lambda n: "plain string"),
    # --- must raise BucketBatchError --------------------------------------------------
    # THE BUG: a 200 that reports a partially applied batch.
    "partial200": (
        200,
        lambda n: {
            "success": True,
            "processed": n,
            "succeeded": max(n - 2, 0),
            "failed": [
                {"path": "a.txt", "error": "internal error"},
                {"path": "sub/b.bin", "error": "xet hash not found"},
            ],
        },
    ),
    # A failure the server counts but does not itemize.
    "unlisted200": (200, lambda n: {"success": True, "processed": 3, "succeeded": 1, "failed": []}),
    # success:false with nothing itemized and counters that agree.
    "successfalse": (200, lambda n: {"success": False, "processed": n, "succeeded": n, "failed": []}),
    # The 422 shape confirmed against production.
    "partial422": (
        422,
        lambda n: {
            "success": False,
            "processed": n,
            "succeeded": 0,
            "failed": [{"path": "copied.txt", "error": "file not found in source repo"}],
        },
    ),
    # 25 failures -> message must cap, .failures must keep all of them.
    "many": (
        200,
        lambda n: {
            "success": False,
            "processed": 25,
            "succeeded": 0,
            "failed": [{"path": f"{i}.txt", "error": "boom"} for i in range(25)],
        },
    ),
    # Schema-invalid entries (reviewer's open question).
    "nullfailed": (200, lambda n: {"success": True, "processed": n, "succeeded": 0, "failed": [None]}),
    "failednotlist": (200, lambda n: {"success": True, "processed": n, "succeeded": n, "failed": 7}),
    "failedstrings": (200, lambda n: {"success": True, "processed": n, "succeeded": 0, "failed": ["x", "y"]}),
    # --- must raise a PLAIN HfHubHTTPError (not BucketBatchError) ----------------------
    "unauth401": (401, lambda n: {"error": "Unauthorized"}),
    "notfound404": (404, lambda n: {"error": "Bucket not found"}),
    "server500": (500, lambda n: {"error": "Internal Server Error"}),
}

# Scenarios whose body is not JSON at all.
RAW_SCENARIOS = {
    "malformed": (200, b"not json at all"),
    "truncated": (200, b'{"success": true, "proc'),
    "htmlgateway": (502, b"<html><body>502 Bad Gateway</body></html>"),
    "emptybody": (200, b""),
    "nullbody": (200, b"null"),
}

# Error responses that carry the Hub's `X-Error-Code` headers. These drive the specialised
# exception types in `utils/_http.py` (BucketNotFoundError, GatedRepoError, ...) and are the real
# regression surface: `_raise_on_bucket_batch_failures` now runs BEFORE `hf_raise_for_status`, so
# it must not swallow or alter any of them.
# scenario -> (status, headers, body)
ERROR_SCENARIOS = {
    # THE critical one: a missing bucket must still surface as BucketNotFoundError.
    "err_bucket404": (404, {"X-Error-Code": "RepoNotFound"}, {"error": "Repository not found"}),
    # Same error code on a 401 (the Hub does this for private/missing repos).
    "err_bucket401": (401, {"X-Error-Code": "RepoNotFound"}, {"error": "Repository not found"}),
    "err_gated": (403, {"X-Error-Code": "GatedRepo"}, {"error": "Access to this repo is gated"}),
    "err_disabled": (403, {"X-Error-Code": "RepoDisabled"}, {"error": "Repo disabled"}),
    "err_revision": (404, {"X-Error-Code": "RevisionNotFound"}, {"error": "Revision not found"}),
    "err_entry": (404, {"X-Error-Code": "EntryNotFound"}, {"error": "Entry not found"}),
    "err_badrequest": (400, {"X-Error-Message": "Bad request: something is off"}, {"error": "bad request"}),
    "err_plain401": (401, {}, {"error": "Unauthorized"}),
    "err_plain403": (403, {}, {"error": "Forbidden"}),
    "err_plain409": (409, {}, {"error": "Conflict"}),
    "err_plain429": (429, {}, {"error": "Too Many Requests"}),
    "err_plain400": (400, {}, {"error": "Bad Request"}),
    # An error status that ALSO carries a batch-shaped body: the body must not win over the status
    # handling for codes the client maps to a specific exception.
    "err_404_with_batch_body": (
        404,
        {"X-Error-Code": "RepoNotFound"},
        {"success": False, "processed": 2, "succeeded": 0, "failed": [{"path": "a.txt", "error": "gone"}]},
    ),
}

# `chunkfail` needs state: succeed on the first chunk, fail on the second (fail-fast check).
_chunk_calls = {"n": 0}

BATCH_RE = re.compile(r"^/api/buckets/([^/]+)/([^/]+)/batch$")
TREE_RE = re.compile(r"^/api/buckets/([^/]+)/([^/]+)/tree")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the harness output readable

    def _send(self, status, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Serve the tree listing so `hf buckets rm --recursive` can resolve paths.
        if TREE_RE.match(self.path.split("?")[0]):
            self._send(
                200,
                [
                    {"type": "file", "path": "a.txt", "size": 10, "xetHash": "a" * 64},
                    {"type": "file", "path": "sub/b.bin", "size": 20, "xetHash": "b" * 64},
                ],
            )
            return
        self._send(404, {"error": "not mocked"})

    def do_POST(self):
        match = BATCH_RE.match(self.path)
        if not match:
            self._send(404, {"error": "not mocked"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        n_ops = len([line for line in raw.split(b"\n") if line.strip()])

        scenario = match.group(2)

        if scenario == "chunkfail":
            _chunk_calls["n"] += 1
            if _chunk_calls["n"] == 1:
                self._send(200, {"success": True, "processed": n_ops, "succeeded": n_ops, "failed": []})
            else:
                self._send(
                    200,
                    {
                        "success": True,
                        "processed": n_ops,
                        "succeeded": n_ops - 1,
                        "failed": [{"path": "chunk2-victim.txt", "error": "boom in chunk 2"}],
                    },
                )
            return

        if scenario in ERROR_SCENARIOS:
            status, headers, body = ERROR_SCENARIOS[scenario]
            self._send(status, body, headers)
            return

        if scenario in RAW_SCENARIOS:
            status, body = RAW_SCENARIOS[scenario]
            self._send(status, body)
            return

        if scenario in SCENARIOS:
            status, builder = SCENARIOS[scenario]
            self._send(status, builder(n_ops))
            return

        self._send(200, {"success": True, "processed": n_ops, "succeeded": n_ops, "failed": []})


def serve(port=0):
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    srv = serve(port)
    print(f"http://127.0.0.1:{srv.server_address[1]}", flush=True)
    threading.Event().wait()
