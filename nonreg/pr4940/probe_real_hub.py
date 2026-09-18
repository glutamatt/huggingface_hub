"""Non-regression probe for PR #4940 against the REAL Hub.

Creates a throwaway private bucket under the logged-in user, drives `batch_bucket_files`
through a set of scenarios, and records the RAW `/batch` response body for each one.

The point is to learn what the production server actually puts in that body, because the
fix in #4940 decides whether to raise from exactly those fields. Two risks we are hunting:
  * false positive  -> a batch that really succeeded now raises (breaks working uploads)
  * false negative  -> a batch that really failed still passes silently (bug not fixed)

Nothing outside the throwaway bucket is touched. The bucket is deleted at the end.
"""

import json
import os
import sys
import time
import traceback

from huggingface_hub import HfApi
from huggingface_hub import hf_api as hf_api_mod
from huggingface_hub.errors import BucketBatchError


CALLS = []  # raw record of every POST .../batch


def instrument():
    """Wrap `http_backoff` so we see the exact body the server sends back."""
    original = hf_api_mod.http_backoff

    def wrapper(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if "/batch" in str(url):
            body = response.text
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = "<not JSON>"
            CALLS.append(
                {
                    "status": response.status_code,
                    "raw": body[:2000],
                    "parsed": parsed,
                    "sent_ops": (kwargs.get("content") or b"").decode("utf-8", "replace").strip().count("\n") + 1
                    if kwargs.get("content")
                    else 0,
                }
            )
        return response

    hf_api_mod.http_backoff = wrapper


def run(label, fn):
    """Run one scenario, print what the SDK did and what the server said."""
    before = len(CALLS)
    print(f"\n{'=' * 78}\nSCENARIO: {label}\n{'=' * 78}")
    outcome = {"label": label}
    try:
        fn()
        print("SDK result: returned normally (no exception)")
        outcome["sdk"] = "ok"
    except BucketBatchError as e:
        print(f"SDK result: raised BucketBatchError")
        print(f"  message: {e}")
        print(f"  .failures: {getattr(e, 'failures', '<missing>')}")
        outcome["sdk"] = "BucketBatchError"
        outcome["message"] = str(e)
        outcome["failures"] = getattr(e, "failures", None)
    except Exception as e:  # noqa: BLE001 - we want to see anything the SDK throws
        print(f"SDK result: raised {type(e).__name__}: {str(e)[:400]}")
        outcome["sdk"] = type(e).__name__
        outcome["message"] = str(e)[:400]

    for call in CALLS[before:]:
        print(f"  --> HTTP {call['status']}, {call['sent_ops']} op(s) sent")
        print(f"      body: {call['raw']}")
    outcome["calls"] = CALLS[before:]
    return outcome


def main():
    instrument()
    api = HfApi()
    user = api.whoami()["name"]
    bucket_id = f"{user}/nonreg-batch-{int(time.time())}"

    print(f"Creating throwaway private bucket: {bucket_id}")
    api.create_bucket(bucket_id, private=True)
    results = []
    try:
        # --- baseline: a batch that must fully succeed -------------------------------
        results.append(
            run(
                "add 3 files (must SUCCEED - false positive check)",
                lambda: api.batch_bucket_files(
                    bucket_id,
                    add=[(b"alpha content", "a.txt"), (b"beta content", "b.txt"), (b"gamma content", "sub/c.txt")],
                ),
            )
        )

        print("\nBucket now contains:", [f.path for f in api.list_bucket_tree(bucket_id, recursive=True)])

        # --- delete a file that exists: must succeed ---------------------------------
        results.append(
            run(
                "delete 1 existing file (must SUCCEED)",
                lambda: api.batch_bucket_files(bucket_id, delete=["a.txt"]),
            )
        )

        # --- THE regression question: delete a path that does not exist --------------
        results.append(
            run(
                "delete 1 MISSING file (regression risk: did this silently pass before?)",
                lambda: api.batch_bucket_files(bucket_id, delete=["does-not-exist.txt"]),
            )
        )

        # --- delete the same file twice in one batch ---------------------------------
        results.append(
            run(
                "delete existing + missing in one batch",
                lambda: api.batch_bucket_files(bucket_id, delete=["b.txt", "also-missing.txt"]),
            )
        )

        # --- re-add an existing path (overwrite): must succeed ------------------------
        results.append(
            run(
                "overwrite an existing path (must SUCCEED)",
                lambda: api.batch_bucket_files(bucket_id, add=[(b"gamma v2", "sub/c.txt")]),
            )
        )

        # --- copy with a bogus xet hash: server should reject -------------------------
        results.append(
            run(
                "copy with a bogus xet hash (should FAIL)",
                lambda: api.batch_bucket_files(
                    bucket_id,
                    copy=[("bucket", bucket_id, "0" * 64, "copied.txt")],
                ),
            )
        )

        # --- a larger batch, to read the counters -------------------------------------
        results.append(
            run(
                "add 25 files (counter shape on a bigger batch)",
                lambda: api.batch_bucket_files(
                    bucket_id, add=[(f"file {i}".encode(), f"many/{i}.txt") for i in range(25)]
                ),
            )
        )

        # --- empty-ish / odd paths ------------------------------------------------------
        results.append(
            run(
                "delete a path with traversal-ish characters (should FAIL or no-op)",
                lambda: api.batch_bucket_files(bucket_id, delete=["../escape.txt"]),
            )
        )

    finally:
        print(f"\n{'=' * 78}\nCLEANUP: deleting {bucket_id}\n{'=' * 78}")
        try:
            api.delete_bucket(bucket_id)
            print("bucket deleted OK")
        except Exception:
            traceback.print_exc()
            print(f"!! FAILED to delete {bucket_id} - delete it manually")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_hub_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nresults written to {out}")


if __name__ == "__main__":
    sys.exit(main())
