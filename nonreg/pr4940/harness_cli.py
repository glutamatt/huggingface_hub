"""Drive the real `hf buckets` CLI against the mock server as a subprocess.

This is the end-to-end check for the `CLI_ERROR_MAPPINGS` half of the fix: a reported batch
failure must reach the user as a readable message with a non-zero exit code, not as a Python
traceback and not as a green success line.
"""

import os
import subprocess
import sys

sys.path.insert(0, "/home/matt/.claude/jobs/00c35840/tmp")

from mock_bucket_server import serve  # noqa: E402


HF = os.path.join(os.path.dirname(sys.executable), "hf")

CASES = [
    # (label, argv, expect_exit_zero, must_contain, must_not_contain)
    (
        "rm on a batch that succeeds",
        ["buckets", "rm", "user/success/a.txt", "-y"],
        True,
        ["path=a.txt", "bucket_id=user/success"],
        ["Traceback"],
    ),
    (
        "rm on a 200 partial failure (THE BUG)",
        ["buckets", "rm", "user/partial200/a.txt", "-y"],
        False,
        ["a.txt", "internal error", "xet hash not found"],
        ["Traceback", "path=a.txt bucket"],
    ),
    (
        "rm on a 422 partial failure",
        ["buckets", "rm", "user/partial422/a.txt", "-y"],
        False,
        ["copied.txt", "file not found in source repo"],
        ["Traceback", "path=a.txt bucket"],
    ),
    (
        "rm on a failure the server does not itemize",
        ["buckets", "rm", "user/unlisted200/a.txt", "-y"],
        False,
        ["not listed by the server"],
        ["Traceback", "path=a.txt bucket"],
    ),
    (
        "rm with 25 failures (message capped)",
        ["buckets", "rm", "user/many/a.txt", "-y"],
        False,
        ["and 15 more"],
        ["Traceback", "path=a.txt bucket"],
    ),
    (
        "rm on 401 (ordinary HTTP error still readable)",
        ["buckets", "rm", "user/unauth401/a.txt", "-y"],
        False,
        [],
        ["Traceback"],
    ),
    (
        "recursive rm on a 200 partial failure",
        ["buckets", "rm", "user/partial200", "-R", "-y"],
        False,
        ["internal error"],
        ["Traceback", "files_deleted"],
    ),
    (
        "recursive rm that succeeds",
        ["buckets", "rm", "user/success", "-R", "-y"],
        True,
        ["files_deleted=2"],
        ["Traceback"],
    ),
    (
        "dry-run must not call the endpoint at all",
        ["buckets", "rm", "user/partial200/a.txt", "--dry-run"],
        True,
        ["dry run"],
        ["Traceback", "internal error"],
    ),
]


def main():
    server = serve()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    env = {
        **os.environ,
        "HF_ENDPOINT": endpoint,
        "HF_TOKEN": "fake-token-for-mock",
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }

    print(f"mock server: {endpoint}\nCLI: {HF}\n")
    failures = []

    for label, argv, expect_zero, must_contain, must_not_contain in CASES:
        proc = subprocess.run([HF, *argv], env=env, capture_output=True, text=True, timeout=120)
        combined = proc.stdout + proc.stderr
        problems = []

        zero = proc.returncode == 0
        if zero != expect_zero:
            problems.append(f"exit code {proc.returncode} (expected {'0' if expect_zero else 'non-zero'})")
        for needle in must_contain:
            if needle not in combined:
                problems.append(f"missing {needle!r}")
        for needle in must_not_contain:
            if needle in combined:
                problems.append(f"unexpectedly contains {needle!r}")

        print("=" * 78)
        print(f"{'PASS' if not problems else 'FAIL'}  {label}")
        print(f"      $ hf {' '.join(argv)}")
        print(f"      exit={proc.returncode}")
        for line in combined.strip().splitlines():
            print(f"      | {line}")
        if problems:
            print(f"      PROBLEMS: {problems}")
            failures.append((label, problems))

    print("\n" + "=" * 78)
    print(f"RESULT: {len(CASES) - len(failures)}/{len(CASES)} CLI cases passed")
    if failures:
        for label, problems in failures:
            print(f"  - {label}: {problems}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
