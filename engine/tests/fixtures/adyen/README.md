# vendored adyen subset

## Adyen/adyen-openapi (MIT, copyright Adyen bv; `LICENSE.adyen-openapi`)

- https://github.com/Adyen/adyen-openapi, `json/Webhooks-v1.json`
- commit `d449f090b9a4842b63b562cd057fe12dfbf4bcad`, fetched 2026-11-09
- `webhooks-v1.subset.json`: `info`; every webhook tagged `Dispute` (name, summary,
  request schema ref) as `x-scruple-dispute-webhooks`; the schemas `Amount`,
  `ChargebackNotificationRequest`, `DisputesNotificationRequestItemWrapper`,
  `DisputesNotificationRequestItem`, `DisputesNotificationAdditionalData`; and every
  example those webhooks reference, verbatim. The examples' `hmacSignature` values are
  truncated placeholders in the source, so tests re-sign them with a dummy key.

## Adyen/adyen-python-api-library (MIT, copyright Adyen; `LICENSE.adyen-python-api-library`)

- https://github.com/Adyen/adyen-python-api-library
- commit `2b708a439f9b95c349f98f2985cda7fca46435bf` (2026-09-17), cloned 2026-11-09 with
  `GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1`
- `*_notification.json`: copied verbatim from `test/mocks/util/`; they verify with the
  key published in `test/UtilTest.py` (`test_webhooks_with_slashes`).
- `hmac_payload_vector.txt`: the exact payload string literal from
  `UtilTest.test_is_valid_hmac_payload` (extracted with `ast`, whitespace preserved).
- The HMAC code in `scruple/ingest/adyen.py` is a port of `Adyen/util.py`.

`extract.py` rebuilds every file here from the two checkouts.
