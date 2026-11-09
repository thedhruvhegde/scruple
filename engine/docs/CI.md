# scruple in CI

The gate runs on every pull request that changes a candidate rule. It replays the
candidate against held-out synthetic cases with the verifiers, writes a JSON
scorecard, comments on the pull request and fails the check on block. All numbers
come from the synthetic simulator; nothing here has been validated on real data.

## The CLI

```bash
scruple ci run --config scruple.toml          # exit 0 pass, 1 gate failed, 2 bad input
scruple ci render --scorecard s.json          # markdown (the PR comment body)
scruple ci comment --scorecard s.json --repo owner/name --pr 12   # needs GITHUB_TOKEN
scruple ci status --scorecard s.json --sha <full sha> [--kind status|check-run] [--post]
```

`scruple.toml`:

```toml
[candidate]
file = "rules/candidate.json"   # same shape as `scruple propose`: rule_id, spec, owner, provenance

[gate]                          # any GateConfig field; defaults are illustrative
n_cases = 3000

[ci]
fail_on_hold = true             # hold = not enough evidence; fails by default (D-010)
scorecard = "scruple-scorecard.json"
db = ":memory:"                 # in-memory db + fixed clock: reruns give identical bytes
```

The scorecard has no timestamps. With the default in-memory database the clock is
fixed, so the same candidate and config always give the same scorecard, decision
hashes and audit head. A reviewer can rerun the check and diff the file.

## GitHub

```yaml
# .github/workflows/scruple.yml
name: scruple
on:
  pull_request:
    paths: ["rules/**", "scruple.toml"]
permissions:
  contents: read
  pull-requests: write   # PR comment
  statuses: write        # commit status "scruple/gate"
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: <owner>/<scruple repo>/.github/actions/scruple-gate@<ref>
        with:
          config: scruple.toml
```

Inputs (all optional): `config`, `candidate`, `scorecard`, `python-version`,
`install` (pip requirement; empty installs the scruple copy the action ships
with), `comment`, `status`, `status-context`, `github-token`. Outputs: `verdict`,
`exit-code`, `scorecard`.

The action writes the markdown to the job summary, upserts one PR comment (marked
`<!-- scruple-gate -->`, so later pushes edit it instead of adding more), posts a
commit status and then exits with the gate's code. Commenting and status posting
are `continue-on-error`: a read-only token (for example on pull requests from
forks) loses the comment, never the verdict. Inputs reach scripts only through
environment variables, never `${{ }}` inside `run:` (tested).

## Required status check (branch protection)

Two ways to make the gate block merging. Pick one.

1. **Commit status `scruple/gate`** (the action's default). Its name does not depend
   on the workflow or job name, so renaming the workflow does not silently drop the
   requirement.
2. **The job's own check run.** Simpler, but the required name is the job name, and
   it changes if the job is renamed.

Setup on GitHub (repository admin): Settings, Branches, add a branch protection rule
(or a ruleset) for the default branch, enable "Require status checks to pass before
merging", and pick `scruple/gate` from the list. A check appears in that list only
after it has run at least once on the repository, so open one pull request first.
Enable "Require branches to be up to date before merging" if candidates interact
(two rules that each pass alone can conflict). GitHub documentation:
https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches

`scruple ci status` prints the exact payload and, with `--post`, sends it:

- `--kind status` (POST `/repos/{o}/{r}/statuses/{sha}`): `state` success or
  failure, `context`, `description` (bounded to 140 characters), `target_url`.
- `--kind check-run` (POST `/repos/{o}/{r}/check-runs`; needs a GitHub App token or
  the Actions token with `checks: write`): `conclusion` success, failure, or
  neutral for a hold that `fail_on_hold = false` lets through; `output.summary` is
  the PR comment markdown.

`--mirror-exit` makes the command exit 1 when the gate failed, for CI systems that
read the step result rather than a posted status.

## GitLab

```yaml
# .gitlab-ci.yml
include:
  - local: ".gitlab/scruple-gate.yml"   # vendored copy, or `remote:` with a URL
variables:
  SCRUPLE_INSTALL: "git+https://<host>/<path>/scruple.git@<ref>"
```

The `scruple-gate` job runs on merge requests and the default branch, keeps
`scruple-scorecard.json` and `scruple-scorecard.md` as artifacts even when it fails,
and exits with the gate's code. To make it required, enable the project's merge
check that pipelines must succeed (Settings, Merge requests). The setting's exact
name and location vary by GitLab version; check GitLab's documentation for yours.
The template's script is run locally in the tests against the real CLI.

## After merge: shadow, canary, approval, evidence

All of these go through `AgentGate`; every state change is an audit event in the
one hash chain, so `scruple audit verify` covers them.

- **Shadow mode** (`AgentGate.shadow_recorder`, `POST /shadow/rules/{id}/versions/{v}/decisions`):
  the production agent posts decisions it already made; the recorder replays them
  in a private simulator under the candidate and accumulates per-reason-code
  statistics. It writes nothing: the module imports no storage, registry, audit or
  service code, and the tests count every storage write (zero).
- **Canary ramp** (`start_canary_ramp`, `advance_canary`, `canary_arm`): case ids
  hash into 10,000 buckets salted by the candidate's content hash; a stage at p%
  sends buckets below p x 100 to the candidate. Stable, monotone across stages,
  and independent between candidates (property-tested). Stages must stay below
  100%, since the last stage still needs a control group. Progress lives only in
  `canary.*` audit events and is rebuilt from them after a restart.
- **Breach**: hard violations on canary cases, or treatment cost above control by
  more than the tolerance, rejects the version in one transaction (decision,
  `canary.breached`, status change). `breach_canary` is the manual kill switch.
- **Approvals** (`set_approval_policy`, `request_approval`, `grant_approval`): N of
  M named approvers, optionally one with a required role; the proposer, owner and
  learning agent can never approve; grants and requests expire. When the policy is
  in force, the single-approver `approve` is refused.
- **Evidence pack** (`scruple ci evidence --db gate.db RULE VERSION`, or
  `scruple ci run --evidence DIR`): sorted JSON with scorecard, statistics,
  decisions, ramp, approvals, config hash, seed commitments and an audit segment
  whose hashes recompute to the chain head; plus a PDF rendered from it. Seeds are
  never exported.

Statistics are injected through `scruple.gate.provider.StatsProvider`; the default
is the existing seeded bootstrap. A sequential test can replace it without changing
the canary or the scorecard.

## What is not done

- No GitLab merge request note: the markdown is an artifact only.
- The baseline policy in CI is the synthetic library baseline, not a repository's own
  active rules.
- One candidate per run.
- Nothing here was run on github.com or gitlab.com; HTTP is tested through a fake
  transport only.
