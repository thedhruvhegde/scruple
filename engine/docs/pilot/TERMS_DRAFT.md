# Pilot terms and data processing outline

> **DRAFT FOR COUNSEL REVIEW. NOT LEGAL ADVICE. DO NOT SEND AS IS.**
>
> This is an outline written by the founder's engineering side to brief a lawyer. It
> lists the points the pilot needs covered and the positions scruple would like to
> take. It has not been reviewed by counsel. Clause wording, governing law, liability,
> data protection obligations (for example under GDPR, UK GDPR, CCPA/CPRA, GLBA) and
> card network or PCI DSS obligations must be decided by qualified counsel for each
> deal. Nothing here states what any law or network rule requires.

Placeholders are in `[brackets]`.

## Part A: pilot agreement

### 1. Parties

- `[scruple legal entity, address]` ("scruple").
- `[Partner legal entity, address]` ("Partner").

### 2. Purpose

A pilot of scruple's verification and release-gate software on Partner's card-dispute
data, as described in the pilot scope (docs/pilot/SCOPE.md, attached as Exhibit A),
to evaluate the criteria in the signed success criteria document (Exhibit B).

### 3. Term

- Starts on `[date]`, runs for six weeks of active work, ends at the readout or on
  `[date]`, whichever is first.
- Either party may end it early by written notice, including for the exit criteria in
  Exhibit A. Sections on confidentiality, deletion, IP and liability survive.

### 4. Fee

- `[Fee: to be set by the founder; see docs/pilot/README.md for the proposals]`,
  invoiced `[on signature / 50% on signature, 50% at readout]`, payable within `[N]` days.
- `[If option A: credit of the fee against a subscription signed within N days.]`
- `[If option B: subscription price that applies if the criteria pass.]`
- `[If option C: publication rights in exchange for the reduced fee, see section 9.]`
- The fee does not depend on the pilot result.
- Partner's own infrastructure and any LLM inference costs stay with Partner.

### 5. What each side provides

- scruple: the software, the work in Exhibit A, the readout.
- Partner: the people and hours in Exhibit A, the data in the data checklist
  (docs/pilot/DATA_CHECKLIST.md, Exhibit C), access needed to install the CI check.

### 6. Data use limited to the pilot

- scruple uses Partner data only to perform the pilot for Partner.
- **No training:** scruple does not use Partner data, or anything derived from it that
  could identify Partner or its customers, to train, fine-tune or evaluate any machine
  learning model, its own or a third party's.
- No sale, sharing or combination of Partner data with other customers' data.
- Aggregate, non-identifying learnings about the software itself (for example "the
  ingest mapping needed a new status value") may be kept. `[Counsel to define precisely.]`

### 7. Confidentiality

- Mutual. Covers Partner data, rules and agent configurations, results, and scruple's
  non-public code and documents.
- `[Standard exclusions, term of N years after the pilot, compelled disclosure clause.]`

### 8. Intellectual property

- Partner keeps all rights in its data, rules, prompts and agent configurations.
- scruple keeps all rights in its software, including improvements made during the
  pilot that do not contain Partner data or confidential information.
- Feedback may be used by scruple without obligation. `[Counsel to confirm wording.]`
- The readout belongs to Partner; scruple may keep a copy under confidentiality.

### 9. Publicity

- No use of Partner's name or logo without prior written consent.
- `[Option C only: scruple may publish an anonymised readout after Partner approves the final text.]`

### 10. Deletion

- Within `[N]` days of the end of the pilot, scruple deletes all Partner data it holds,
  including databases, exports, backups and keys it holds, and confirms in writing
  (docs/pilot/READOUT_TEMPLATE.md section 10).
- Because scruple's audit log is append-only by design, deletion is done by deleting
  the whole pilot database and the keys, not individual records (Exhibit D).

### 11. Warranties and disclaimers

- The software is provided for evaluation `[as is]`. Results on synthetic data are not
  a statement about Partner's results.
- scruple does not provide legal, regulatory, card network or financial advice.
- scruple acts on nothing in Partner's production systems: the pilot is read-only
  (no writes to Stripe, Adyen or any network) and shadow mode acts on no decision.

### 12. Liability

- Cap: `[placeholder, for counsel: for example the fees paid under this agreement]`.
- Exclusions and carve-outs (confidentiality, data breach, indemnities):
  `[for counsel]`.

### 13. General

- Governing law and venue: `[for counsel]`.
- Entire agreement, order of precedence (this agreement, then the DPA, then exhibits),
  assignment, notices: `[for counsel]`.

## Part B: data processing addendum (outline)

### B1. Roles

- Partner is `[controller / business]`; scruple is `[processor / service provider]`
  for the pilot. `[Counsel to confirm the terms for the applicable law.]`

### B2. Processing details

| item | content |
|---|---|
| subject matter | evaluation of dispute decision software |
| duration | the pilot term plus the deletion period |
| nature and purpose | parsing, validation, redaction, tokenisation, replay and scoring of dispute records |
| categories of data | dispute and payment identifiers, amounts, currency, dates, statuses, reason codes, evidence field names, balance movements (Exhibit C). No card numbers, cardholder names or free text are stored, by design |
| data subjects | `[cardholders and merchants, to the extent identifiers relate to them; counsel to confirm]` |
| location | Partner's environment by default; otherwise `[scruple machine and region]` |

### B3. Processor obligations (headings for counsel)

- Process only on documented instructions (this addendum and Exhibit A).
- Confidentiality of personnel with access.
- Security measures: Exhibit D (docs/pilot/SECURITY.md), including its "Not yet" list,
  which Partner acknowledges.
- Assistance with data subject requests and with security incidents.
- Breach notification within `[N hours]`.
- Deletion or return at the end (Part A section 10).
- Audit and information rights: `[for counsel; scruple can provide the audit log and
  evidence packs for the pilot]`.

### B4. Subprocessors

- **None by default.** When scruple runs in Partner's environment, scruple processes no
  Partner data on its own systems.
- If work runs on a scruple-operated machine: the hosting provider `[name, region]` is
  the only subprocessor, listed here before any data moves, with Partner's prior written
  approval required for any change.
- No LLM API provider receives Partner data unless Partner runs that inference on its
  own account under its own terms.

### B5. International transfers

`[For counsel, if any data leaves Partner's jurisdiction.]`

## Exhibits

| exhibit | document |
|---|---|
| A | pilot scope: docs/pilot/SCOPE.md |
| B | signed success criteria: docs/pilot/SUCCESS_CRITERIA.md |
| C | data checklist: docs/pilot/DATA_CHECKLIST.md |
| D | security measures: docs/pilot/SECURITY.md |
