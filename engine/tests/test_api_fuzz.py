"""API fuzzing from the OpenAPI schema (roadmap 166), with schemathesis in-process.

Every route the app publishes is called with inputs generated from its own schema
(valid and invalid), against a real gate on in-memory SQLite, and every response
is checked: no 5xx ever; bodies and headers conform to the documented schema;
schema-invalid input is rejected; every 4xx is JSON with a non-empty `detail`.

Checks left out, and why:
- `status_code_conformance`: error statuses (403/404/409) are checked by
  `test_documented_error_statuses` against an explicit table instead;
- `allow_header_conformance`: on a 405 Starlette lists only the methods of the
  route that matched the path (e.g. POST for /rules, not GET), a framework detail;
- `positive_data_acceptance` on the Stripe webhook (raw bytes verified against a
  signature, so "schema-valid" unsigned bodies must get 400) and on POST /rules
  (a condition's value type depends on its field, which JSON Schema cannot say),
  and on the imported-shadow routes (ISO 4217 membership, stage and state agreement).

Synthetic data only.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import schemathesis
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from schemathesis.checks import CheckContext
from schemathesis.core.failures import Failure

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.limits import Limits
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

SECRETS_ENV = "SCRUPLE_STRIPE_WEBHOOK_SECRETS"


def _app() -> Any:
    gate = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=200, bootstrap_resamples=100, canary_n_cases=200),
    )
    gate.bootstrap_baseline()
    gate.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    old = os.environ.get(SECRETS_ENV)
    os.environ[SECRETS_ENV] = "whsec_fuzzing-secret-0123456789"  # read once, at creation
    try:
        # Development-only X-Actor trust: the fuzzer explores the gate's inputs, not
        # credentials (tests/test_auth_*.py).
        # Body size limits off too: the schema cannot express them, and generated
        # bodies exceed 2 MiB (tests/test_auth_ratelimit.py covers the 413).
        return create_app(
            gate,
            auth=Authenticator(dev_actor_header=True),
            limits=Limits(max_body_bytes=2**40),
        )
    finally:
        if old is None:
            del os.environ[SECRETS_ENV]
        else:
            os.environ[SECRETS_ENV] = old


APP = _app()
ACTOR = {"X-Actor": "model-risk@demo.bank"}
# Bodies whose validity the schema cannot express: a raw signed webhook, rules
# whose condition value type depends on the field (`mcc` needs a string, ...), and
# imported cases, whose currency must be a listed ISO 4217 code and whose state must
# agree with the stage (tests/test_gate_shadow_imported.py covers valid batches).
IMPORTED = "/shadow/rules/{rule_id}/versions/{version}/imported"
CROSS_FIELD_OR_RAW = {
    "/ingest/stripe/webhook",
    "/rules",
    f"{IMPORTED}/decisions",
    f"{IMPORTED}/outcomes",
}
schema = schemathesis.openapi.from_asgi("/openapi.json", APP, headers=ACTOR)


def client_errors_are_well_formed(ctx: CheckContext, response: Any, case: Any) -> bool | None:
    if 400 <= response.status_code < 500:
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if not detail or not isinstance(detail, str | list):
            raise Failure(
                operation=case.operation.label,
                title="malformed client error",
                message=f"{response.status_code} body {response.text[:200]!r}",
            )
    return None


@schema.parametrize()
@settings(max_examples=30, deadline=None, suppress_health_check=list(HealthCheck))
def test_every_route_survives_schema_fuzzing(case: Any) -> None:
    response = case.call(headers=ACTOR)
    excluded = [
        schemathesis.checks.status_code_conformance,
        schemathesis.checks.allow_header_conformance,
    ]
    if case.operation.path in CROSS_FIELD_OR_RAW:
        excluded.append(schemathesis.checks.positive_data_acceptance)
    case.validate_response(
        response, excluded_checks=excluded, additional_checks=[client_errors_are_well_formed]
    )


# --- regressions and explicit error contracts ---------------------------------------
client = TestClient(APP)


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("get", "/rules/no-such-rule", None, 404),
        ("get", "/decisions/dec-99999", None, 404),
        ("get", "/decisions/dec-99999/report", None, 404),
        ("post", "/rules/no-such-rule/versions/1/evaluate", None, 404),
        ("post", f"/rules/{GOOD_RULE_ID}/versions/1/approve", None, 403),
        ("post", "/rules/baseline-x/rollback", {"reason": "no such rule"}, 404),
        ("post", "/ingest/stripe/webhook", None, 400),
    ],
)
def test_documented_error_statuses(
    method: str, path: str, body: dict[str, str] | None, status: int
) -> None:
    kwargs: dict[str, Any] = {"headers": ACTOR}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == status, r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
