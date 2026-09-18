"""What does the production Hub actually put in the BODY of a `/batch` error response?

The fix reads the body before `hf_raise_for_status`, so an error response whose body happens to be
batch-shaped (`success: false`, or a `failed` list, or int counters) gets turned into a
BucketBatchError and never reaches the specialised mapping (BucketNotFoundError & co).

This probe asks production for real error responses and prints status + headers + raw body, so we
can tell whether that hijack is reachable in practice or only synthetic.

Read-only: every operation targets a path that does not exist, in buckets we do not own or that do
not exist at all.
"""

import json

from huggingface_hub import HfApi, get_token
from huggingface_hub.utils import build_hf_headers, http_backoff


PROBE_PATH = "nonexistent-probe-path-do-not-create.txt"

api = HfApi()
user = api.whoami()["name"]

CASES = [
    ("bucket that does not exist (own namespace)", f"{user}/definitely-does-not-exist-{PROBE_PATH[:8]}"),
    ("namespace that does not exist", "this-namespace-does-not-exist-xyz/some-bucket"),
    ("org bucket we have no write access to", "huggingface/definitely-not-a-real-bucket"),
    ("malformed bucket id (no slash handling)", f"{user}/"),
]


def probe(label, bucket_id):
    payload = json.dumps({"type": "deleteFile", "path": PROBE_PATH}).encode() + b"\n"
    headers = {"Content-Type": "application/x-ndjson", **build_hf_headers(token=get_token())}
    url = f"{api.endpoint}/api/buckets/{bucket_id}/batch"
    print("=" * 78)
    print(f"{label}\n  POST {url}")
    try:
        response = http_backoff("POST", url, headers=headers, content=payload, retry_on_status_codes=())
    except Exception as e:  # noqa: BLE001
        print(f"  transport error: {type(e).__name__}: {str(e)[:200]}")
        return
    print(f"  status      : {response.status_code}")
    print(f"  X-Error-Code: {response.headers.get('X-Error-Code')}")
    print(f"  X-Error-Msg : {response.headers.get('X-Error-Message')}")
    print(f"  raw body    : {response.text[:400]!r}")

    # Would the new pre-check hijack this response?
    try:
        parsed = response.json()
    except ValueError:
        print("  -> body is not JSON: pre-check returns early, mapping preserved")
        return
    if not isinstance(parsed, dict):
        print("  -> body is not a JSON object: pre-check returns early, mapping preserved")
        return
    failed = parsed.get("failed")
    failures = failed if isinstance(failed, list) else []
    processed, succeeded = parsed.get("processed"), parsed.get("succeeded")
    unlisted = processed - succeeded - len(failures) if isinstance(processed, int) and isinstance(succeeded, int) else 0
    hijacked = bool(failures) or unlisted > 0 or parsed.get("success") is False
    print(f"  -> batch-shaped? failures={len(failures)} unlisted={unlisted} success={parsed.get('success')!r}")
    print(f"  -> WOULD BE HIJACKED into BucketBatchError: {hijacked}")


for label, bucket_id in CASES:
    probe(label, bucket_id)
