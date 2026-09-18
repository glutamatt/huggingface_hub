"""Run every scenario against a given endpoint and print one JSON blob of results.

Executed twice - once under the pre-fix tree, once under the fixed branch - so the two outputs can
be diffed. Anything that differs outside the intended batch-failure scenarios is a regression.
"""

import json
import sys

from huggingface_hub import HfApi


SCENARIOS = [
    # error statuses: these MUST be identical before and after the fix
    "err_bucket404",
    "err_bucket401",
    "err_gated",
    "err_disabled",
    "err_revision",
    "err_entry",
    "err_badrequest",
    "err_plain401",
    "err_plain403",
    "err_plain409",
    "err_plain429",
    "err_plain400",
    "err_404_with_batch_body",
    "htmlgateway",
    "server500",
    "notfound404",
    "unauth401",
    # success bodies: these MUST be identical (no false positives introduced)
    "success",
    "empty200",
    "nofields",
    "jsonlist",
    "jsonstring",
    "malformed",
    "truncated",
    "emptybody",
    "nullbody",
    "failednotlist",
    # batch-failure bodies: these are the INTENDED behaviour change
    "partial200",
    "unlisted200",
    "successfalse",
    "partial422",
    "many",
    "nullfailed",
    "failedstrings",
]

INTENDED_CHANGE = {
    "partial200",
    "unlisted200",
    "successfalse",
    "partial422",
    "many",
    "nullfailed",
    "failedstrings",
}


def main():
    endpoint = sys.argv[1]
    api = HfApi(endpoint=endpoint, token="fake-token-for-mock")
    results = {}
    for scenario in SCENARIOS:
        try:
            api.batch_bucket_files(f"user/{scenario}", delete=["a.txt", "sub/b.bin"])
            results[scenario] = {"exc": None, "msg": None}
        except Exception as e:  # noqa: BLE001 - capturing whatever comes out is the point
            results[scenario] = {
                "exc": type(e).__name__,
                "msg": str(e),
                "mro": [c.__name__ for c in type(e).__mro__[:5]],
                "bucket_id": getattr(e, "bucket_id", "<none>"),
            }
    print("###JSON###")
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
