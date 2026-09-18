"""A/B the fixed branch against the pre-fix tree on identical mock responses.

Both runs hit the SAME mock server, so any difference in exception type or message comes from the
patch and nothing else. Differences are expected ONLY for the batch-failure scenarios.
"""

import json
import subprocess
import sys

sys.path.insert(0, "/home/matt/.claude/jobs/00c35840/tmp")

from ab_runner import INTENDED_CHANGE  # noqa: E402
from mock_bucket_server import serve  # noqa: E402


BASE_PY = "/home/matt/.claude/jobs/00c35840/tmp/base_repo/.venv/bin/python"
FIX_PY = (
    "/home/matt/repositories/github.com/huggingface/huggingface_hub/.claude/worktrees/"
    "buckets-nonreg-test/.venv/bin/python"
)
RUNNER = "/home/matt/.claude/jobs/00c35840/tmp/ab_runner.py"


def run(python, endpoint, label):
    proc = subprocess.run([python, RUNNER, endpoint], capture_output=True, text=True, timeout=900)
    if "###JSON###" not in proc.stdout:
        print(f"!! {label} runner produced no JSON")
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        sys.exit(2)
    return json.loads(proc.stdout.split("###JSON###", 1)[1])


def main():
    server = serve()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"mock server: {endpoint}")

    print("running PRE-FIX tree (e8bf4bd2~1) ...")
    before = run(BASE_PY, endpoint, "pre-fix")
    print("running FIXED branch (e8bf4bd2) ...")
    after = run(FIX_PY, endpoint, "fixed")

    unexpected, intended, identical = [], [], []
    for scenario in sorted(before):
        b, a = before[scenario], after[scenario]
        same = b["exc"] == a["exc"] and b.get("msg") == a.get("msg")
        if same:
            identical.append(scenario)
        elif scenario in INTENDED_CHANGE:
            intended.append((scenario, b, a))
        else:
            unexpected.append((scenario, b, a))

    print("\n" + "=" * 78)
    print("UNCHANGED (must include every error status)")
    print("=" * 78)
    for scenario in identical:
        exc = before[scenario]["exc"] or "no exception"
        print(f"  {scenario:<26} -> {exc}")

    print("\n" + "=" * 78)
    print("INTENDED BEHAVIOUR CHANGE (the fix)")
    print("=" * 78)
    for scenario, b, a in intended:
        print(f"  {scenario}")
        print(f"     before: {b['exc']}: {(b['msg'] or '').splitlines()[0] if b['msg'] else '<returned normally>'}")
        print(f"     after : {a['exc']}: {(a['msg'] or '').splitlines()[0] if a['msg'] else '<returned normally>'}")

    print("\n" + "=" * 78)
    if unexpected:
        print(f"REGRESSIONS: {len(unexpected)} scenario(s) changed unexpectedly")
        print("=" * 78)
        for scenario, b, a in unexpected:
            print(f"  {scenario}")
            print(f"     before: {b['exc']}  mro={b.get('mro')}  bucket_id={b.get('bucket_id')}")
            print(f"       msg: {b['msg']}")
            print(f"     after : {a['exc']}  mro={a.get('mro')}  bucket_id={a.get('bucket_id')}")
            print(f"       msg: {a['msg']}")
        return 1

    print("NO REGRESSIONS: every non-batch scenario behaves identically before and after")
    print("=" * 78)
    print(f"  unchanged: {len(identical)}   intended changes: {len(intended)}")

    # Explicit spot-check on the highest-risk one.
    b404 = after["err_bucket404"]
    print(f"\n  err_bucket404 -> {b404['exc']} (bucket_id={b404['bucket_id']})")
    assert b404["exc"] == "BucketNotFoundError", "missing bucket must still raise BucketNotFoundError"
    print("  BucketNotFoundError preserved through the new pre-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
