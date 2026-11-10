# licence: not chosen yet

this repository has no licence. that is a decision for the founder, not for tooling.
until one is chosen:

- `pyproject.toml` has no `license` field and no `License ::` classifier.
- there is no `LICENSE` file, so default copyright applies: all rights reserved. people
  can read published code but have no grant to use, copy, modify or redistribute it.
- do not publish to pypi or npm before deciding. a public package with no licence is
  legally unusable for most companies, and a licence cannot be withdrawn from versions
  already published under it.

## options

| option | what it allows | fits when |
|---|---|---|
| apache-2.0 | use, modify, redistribute, commercial use; explicit patent grant; notice file | the engine or sdks should be adopted widely, including by banks whose legal teams ask about patents |
| mit | the same, shorter, no patent grant | sdks only; maximum adoption, minimum text |
| agpl-3.0 | open source, but anyone running a modified version as a network service must publish their changes | the engine is open but hosted competitors should not be able to fork it silently |
| business source licence 1.1 | source available; production use limited by an "additional use grant"; converts to an open licence after a set date (at most four years) | the engine should be readable and self-hostable for evaluation while commercial hosting stays reserved |
| proprietary (all rights reserved, or a custom eula) | only what a contract grants | pilots run under contract and nothing is published publicly |
| split | e.g. sdks under apache-2.0 or mit, engine under bsl or proprietary | clients should integrate freely while the verifiers and simulator stay the product |

## things to check whichever is chosen

- direct runtime dependencies, from their installed metadata (2026-11-10): fastapi,
  pydantic, sqlalchemy, alembic, mcp, pyyaml, claude-agent-sdk, langgraph are mit; httpx and
  uvicorn are bsd-3-clause; cryptography is apache-2.0 or bsd-3-clause; defusedxml is psf.
  psycopg and fpdf2 are lgpl-3.0-only: fine to depend on unmodified under any of the options
  above, but a proprietary or bsl build that vendors or modifies them needs a lawyer's look.
  transitive dependencies are not checked; run a licence scanner before the first release.
- `data/real/` holds cfpb complaint data, public domain (see `docs/data/CFPB.md`). it is not
  in the sdist or wheel.
- contributions from anyone other than the founder need a cla or dco before the licence
  choice is final, or relicensing later needs their consent.
- once chosen: add `LICENSE`, set `license = "<spdx id>"` and `license-files = ["LICENSE"]`
  in `pyproject.toml`, set `"license"` in `sdk/typescript/package.json`, add it to the sdist
  include list, and delete this file.
