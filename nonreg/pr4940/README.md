# Non-regression testing for PR #4940

[Buckets] Raise when the /batch endpoint reports failed operations — https://github.com/huggingface/huggingface_hub/pull/4940

The PR makes `_batch_bucket_files` read the batch report out of the response body *before*
`hf_raise_for_status`. That is the whole risk surface: the body is now inspected on **every**
`/batch` response, including error ones that previously went straight to the specialised
exception mapping. These harnesses exercise that surface three ways.

## What is here

| File | What it does |
| --- | --- |
| `probe_real_hub.py` | Creates a throwaway private bucket on **production**, drives 8 real scenarios, records the raw `/batch` body for each, deletes the bucket. Answers "what does the server actually send?" |
| `probe_error_bodies.py` | Asks production for real `/batch` **error** responses (missing bucket, missing namespace, no access) and reports whether each body is batch-shaped enough to be hijacked by the new pre-check. Read-only. |
| `mock_bucket_server.py` | Mock of the `/batch` endpoint. Scenario is chosen by the bucket *name*, so the same server serves the SDK harness and real `hf buckets ...` CLI calls. Covers what production won't produce on demand (200-with-partial-failure, malformed bodies, `X-Error-Code` headers). |
| `harness_sdk.py` | Drives the real SDK against the mock over real HTTP — 21 scenarios, each with a declared expected outcome, plus multi-chunk fail-fast. |
| `harness_cli.py` | Drives the real `hf buckets rm` CLI as a subprocess against the mock — 9 cases checking rendered message, exit code, and absence of traceback. |
| `ab_runner.py` / `ab_driver.py` | A/B: runs 34 identical scenarios under the **pre-fix** tree and the **fixed** tree against one mock instance and diffs exception type + message. Anything differing outside the intended batch scenarios is a regression. |
| `real_hub_results.json` | Captured production responses from `probe_real_hub.py`. |

## Running

`probe_real_hub.py` needs a real write token and creates/deletes a bucket in your own namespace.
The mock-based harnesses need no credentials.

```bash
uv venv && uv pip install -e ".[dev]"

.venv/bin/python nonreg/pr4940/harness_sdk.py     # 21/21 expected
.venv/bin/python nonreg/pr4940/harness_cli.py     #  9/9  expected
.venv/bin/python nonreg/pr4940/probe_real_hub.py  # needs a write token

# A/B needs a second venv built from the pre-fix commit:
mkdir -p /tmp/base_repo && git archive <fix-commit>~1 | tar -x -C /tmp/base_repo
cd /tmp/base_repo && uv venv && uv pip install -e ".[dev]"
# then adjust BASE_PY/FIX_PY at the top of ab_driver.py
.venv/bin/python nonreg/pr4940/ab_driver.py       # expect 0 regressions
```

## What production actually returns

Captured from `huggingface.co` on a throwaway bucket:

```
add 3 files          -> 200 {"success":true,"processed":3,"succeeded":3,"failed":[]}
delete existing file -> 200 {"success":true,"processed":1,"succeeded":1,"failed":[]}
delete MISSING file  -> 200 {"success":true,"processed":1,"succeeded":1,"failed":[]}
overwrite a path     -> 200 {"success":true,"processed":1,"succeeded":1,"failed":[]}
add 25 files         -> 200 {"success":true,"processed":25,"succeeded":25,"failed":[]}
copy w/ bogus hash   -> 422 {"success":false,"processed":1,"succeeded":0,
                             "failed":[{"path":"copied.txt","error":"Failed to duplicate file ..."}]}
missing bucket       -> 404 X-Error-Code: RepoNotFound  {"error":"Repository not found"}
```

Two things follow. `processed == succeeded` on every genuine success, so the new check cannot
false-positive on them. And the 422 really does carry the documented batch body, so the PR's
premise for reading the body before `hf_raise_for_status` holds against the live endpoint.
