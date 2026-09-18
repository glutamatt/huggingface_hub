"""Drive the real SDK against the mock `/batch` server, over real HTTP.

Unlike tests/test_buckets.py (which patches at the requests layer), this goes through the whole
client stack: http_backoff, headers, retries, response parsing. Each scenario declares what the
FIXED client must do; anything else is a regression.
"""

import sys

sys.path.insert(0, "/home/matt/.claude/jobs/00c35840/tmp")

from mock_bucket_server import serve  # noqa: E402

from huggingface_hub import HfApi  # noqa: E402
from huggingface_hub.errors import BucketBatchError, HfHubHTTPError  # noqa: E402


# scenario -> (expected outcome, note)
#   "ok"      : must return normally
#   "batch"   : must raise BucketBatchError
#   "http"    : must raise HfHubHTTPError but NOT BucketBatchError
EXPECTATIONS = [
    ("success", "ok", "exact production success payload - the false-positive guard"),
    ("empty200", "ok", "{} - reviewer open question, currently treated as success"),
    ("nofields", "ok", "only {'success': true}"),
    ("jsonlist", "ok", "JSON list body -> left to hf_raise_for_status"),
    ("jsonstring", "ok", "JSON string body -> left to hf_raise_for_status"),
    ("malformed", "ok", "non-JSON 200 -> must not crash"),
    ("truncated", "ok", "truncated JSON 200 -> must not crash"),
    ("emptybody", "ok", "empty 200 body"),
    ("nullbody", "ok", "'null' 200 body"),
    ("partial200", "batch", "THE BUG: 200 reporting a partially applied batch"),
    ("unlisted200", "batch", "failure counted but not itemized"),
    ("successfalse", "batch", "success:false with agreeing counters"),
    ("partial422", "batch", "422 shape confirmed against production"),
    ("many", "batch", "25 failures -> message capped, .failures complete"),
    ("nullfailed", "batch", "failed:[null] - reviewer open question"),
    ("failednotlist", "ok", "failed:7 -> not a list, counters agree"),
    ("failedstrings", "batch", "failed:['x','y'] - non-dict entries"),
    ("unauth401", "http", "401 must stay a plain HfHubHTTPError"),
    ("notfound404", "http", "404 must stay a plain HfHubHTTPError"),
    ("server500", "http", "500 must stay a plain HfHubHTTPError"),
    ("htmlgateway", "http", "HTML 502 must stay a plain HfHubHTTPError"),
]


def main():
    server = serve()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    api = HfApi(endpoint=endpoint, token="fake-token-for-mock")

    passed, failed = 0, []
    print(f"mock server: {endpoint}\n")
    print(f"{'scenario':<16}{'expected':<10}{'actual':<22}{'verdict'}")
    print("-" * 78)

    for scenario, expected, note in EXPECTATIONS:
        bucket = f"user/{scenario}"
        try:
            api.batch_bucket_files(bucket, delete=["a.txt", "sub/b.bin"])
            actual, detail = "ok", ""
        except BucketBatchError as e:
            actual, detail = "batch", str(e)
        except HfHubHTTPError as e:
            actual, detail = "http", str(e).splitlines()[0]
        except Exception as e:  # noqa: BLE001
            actual, detail = f"!{type(e).__name__}", str(e)[:200]

        ok = actual == expected
        passed += ok
        if not ok:
            failed.append((scenario, expected, actual, detail, note))
        print(f"{scenario:<16}{expected:<10}{actual:<22}{'PASS' if ok else 'FAIL'}   {note}")

    # --- detail checks that go beyond the raise/no-raise verdict -----------------------
    print("\n" + "=" * 78)
    print("DETAIL CHECKS")
    print("=" * 78)

    # message cap + full failure list
    try:
        api.batch_bucket_files("user/many", delete=["x"])
    except BucketBatchError as e:
        lines = str(e).splitlines()
        listed = [line for line in lines if line.startswith("  - ") and "more" not in line]
        print(f"[many] .failures length          : {len(e.failures)} (expect 25)")
        print(f"[many] failures listed in message: {len(listed)} (expect 10)")
        print(f"[many] message tail              : {lines[-1]!r}")
        assert len(e.failures) == 25, "full failure list must survive on the exception"
        assert len(listed) == 10, "message must cap at 10 listed failures"

    # partial200 failure payload passthrough
    try:
        api.batch_bucket_files("user/partial200", delete=["a.txt", "sub/b.bin"])
    except BucketBatchError as e:
        print(f"[partial200] .failures           : {e.failures}")
        print(f"[partial200] message             :\n{e}")
        assert e.failures == [
            {"path": "a.txt", "error": "internal error"},
            {"path": "sub/b.bin", "error": "xet hash not found"},
        ]
        assert isinstance(e, HfHubHTTPError), "must stay catchable as HfHubHTTPError (no breaking change)"

    # unlisted failures wording
    try:
        api.batch_bucket_files("user/unlisted200", delete=["a"])
    except BucketBatchError as e:
        print(f"[unlisted200] message            :\n{e}")

    # nullfailed shape (reviewer's open question)
    try:
        api.batch_bucket_files("user/nullfailed", delete=["a"])
    except BucketBatchError as e:
        print(f"[nullfailed] .failures           : {e.failures}  <-- schema-invalid passthrough")
        print(f"[nullfailed] message             :\n{e}")

    # fail-fast across chunks: 1500 deletes -> 2 chunks, 2nd one fails
    print("\n[chunkfail] 1500 deletes -> 2 chunks of 1000/500, server fails the 2nd chunk")
    try:
        api.batch_bucket_files("user/chunkfail", delete=[f"f{i}.txt" for i in range(1500)])
        print("[chunkfail] FAIL - returned normally, second chunk failure was swallowed")
        failed.append(("chunkfail", "batch", "ok", "", "multi-chunk fail-fast"))
    except BucketBatchError as e:
        first = str(e).splitlines()[0]
        print(f"[chunkfail] raised BucketBatchError: {first}")
        print(f"[chunkfail] .failures            : {e.failures}")
        assert "out of 500 operation(s)" in first, f"count should reflect the failing CHUNK, got: {first}"
        print("[chunkfail] PASS - fail-fast on the chunk that failed, count is the chunk size")

    print("\n" + "=" * 78)
    print(f"RESULT: {passed}/{len(EXPECTATIONS)} scenarios matched expectation")
    if failed:
        print("\nFAILURES:")
        for scenario, expected, actual, detail, note in failed:
            print(f"  - {scenario}: expected {expected}, got {actual} ({note})\n      {detail}")
        return 1
    print("all scenarios behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
