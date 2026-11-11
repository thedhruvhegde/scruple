# Business continuity and disaster recovery

Owner: the founder. Review: yearly, and after any restore drill or outage. Not legal
advice; see README.md for the status words.

## What has to keep working

1. A partner's evidence: the audit chain, decisions and rule history must survive and
   stay verifiable. Losing a day of availability is acceptable in a pilot; losing or
   silently changing evidence is not.
2. A partner's ability to run without scruple: the engine is an image and a package
   that runs in the partner's environment, so a partner keeps running if scruple the
   company stops (subject to the licence, docs/V1_PLAN.md A1, not yet chosen).
3. The ability to rebuild: source, lockfile and image recipe in git; images by digest.

## Targets (proposals, not commitments)

| | proposal | why |
|---|---|---|
| RPO, gate database | 24 hours with nightly logical backups; minutes with RDS point-in-time recovery | backup.sh nightly; RDS PITR in the Terraform skeleton |
| RTO, single deployment | 4 hours | restore.sh into an empty database, then smoke test |
| restore drill | one passing drill per quarter | docs/SLOs.md SLI 7 |

None of these has been measured. They become commitments only in a signed agreement.

## Controls

| control | how | status |
|---|---|---|
| verified logical backups | `deploy/scripts/backup.sh` writes the dump, its sha256 and the chain head; `restore.sh` restores only into an empty database and checks checksum, migration head, `scruple audit verify` and the recorded head (tests/test_deploy_backup.py) | code; tested against a local Postgres |
| managed backups | RDS automated backups, 14 days, PITR, final snapshot on prod deletion (deploy/terraform/aws) | written, never applied |
| external record of the chain head | `.head` from backup.sh kept apart from the database; `scruple audit anchor` (RFC 3161 or git) | code; scheduling is process |
| redundancy | Multi-AZ database and two or more pods in the Helm chart and Terraform skeleton | written, never run on a cluster |
| rollback of a bad deploy | Helm history and rollback; ECS circuit breaker (docs/DEPLOY.md) | process; not exercised |
| a second region | | not yet |
| hosted demo | rebuilt from scratch nightly (deploy/demo/reset.sh); no backups by design, synthetic only | recipe, not deployed |

## People

The largest continuity risk is that scruple is one person. Mitigations:

- Everything needed to build, test and deploy is in the repository and its docs; no
  step lives only in someone's head (docs/DEPLOY.md, docs/pilot/RUNBOOK.md).
- Partners run the engine themselves by default; their evidence is in their database.
- **Not yet**: a named backup contact with access to the repository, registry and
  domain, and an escrow or continuity clause in the terms. Both are owner decisions.

## Testing this plan

- Quarterly restore drill: restore last night's dump into a scratch database with
  `restore.sh`, record the time it took and the result. **Not yet done** outside tests.
- Yearly: rebuild the image from a clean checkout, deploy it to staging from scratch
  with the runbook only, note every step that was missing.
