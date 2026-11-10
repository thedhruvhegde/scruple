/** Friendly names for the generated OpenAPI types (src/schema.ts, do not edit that file). */

import type { components, operations, paths } from "./schema.js";

export type { components, operations, paths };

type S = components["schemas"];

export type BuildInfo = S["BuildInfo"];
export type RuleSpec = S["RuleSpec"];
export type Provenance = S["Provenance"];
export type RuleVersion = S["RuleVersion"];
export type RuleStatus = S["RuleStatus"];
export type RegisterIn = S["RegisterIn"];
export type RollbackIn = S["RollbackIn"];
export type DecisionOut = S["DecisionOut"];
export type Decision = S["Decision"];
export type Outcome = S["Outcome"];
export type IntegrityReport = S["IntegrityReport"];
export type LiveDecision = S["LiveDecision"];
export type LiveDecisionBatch = S["LiveDecisionBatch"];
export type BatchResult = S["BatchResult"];
export type ShadowSummary = S["ShadowSummary"];
export type HTTPValidationError = S["HTTPValidationError"];
