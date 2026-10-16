# SLD-18 generated local workflow

This example uses takflow's optional Sledra exporter to generate a synthetic
ecFlow definition and a static `DefinitionManifest`. The generated jobs run
`prepare → forecast → verify` as real local subprocesses. `runner.py` owns
launch, submission, and finish boundaries; each invocation and Attempt has a
unique fixed context path. Payloads read that path from `SLEDRA_CONTEXT`
without copying the context or consulting a mutable current-Run file.

From the Sledra repository, with the sibling takflow repository and the local
Sledra build already installed:

```bash
takflow_python=../../../../framework/takflow/.venv/bin/python
"$takflow_python" examples/generated/generate.py \
  --output /tmp/sledra-generated-bundle --spec-dir spec/1.0.0
PYTHONPATH=analysis/src analysis/.venv/bin/python \
  examples/generated/verify_flow.py \
  --bundle /tmp/sledra-generated-bundle \
  --workspace /tmp/sledra-generated-acceptance \
  --sledra-bin build/bin/sledra
```

`verify_flow.py` compares disabled, enabled, failed Run and Attempt registration,
missing Shell API, missing context, and event writer failure runs. It also checks distinct
Runs for the same definition, different and matching cycles, request reentry,
failure then retry, retry forks, finish followed by a late Attempt, mock
rejection and timeout, accepted submission with local record loss, and payload
start before the acceptance response. The mock scheduler writes accepted
`JobRef` documents under `sledra/mock-jobs/`; rejected and unknown outcomes
have no JobRef. These documents are imported after all business subprocesses
exit. The verifier writes `acceptance.json` and a local `facts.sqlite` in the
requested workspace.

The `verify` job checks `expected_product.json` and writes a local pass/fail
result. A zero exit or product check never emits `delivery.confirmed`.
`finish` declares a covered Attempt set; it does not infer workflow success.
This example does not submit to HPC or connect ecFlow repeat/callbacks. Those
integration points are covered by SLD-19.

The R11 opt-in case is `framework/takflow-test-suite/tests/test_sld18_generated.py`.
Run it with `SLEDRA_REPO` set to this repository's absolute path; it uses the
takflow environment for generation and the Sledra analysis environment for
offline replay.
