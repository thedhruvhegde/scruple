/** Errors thrown by the client. Every HTTP failure is an `ApiError` subclass. */

export class ScrupleError extends Error {
  override name = "ScrupleError";
}

/** The request got no HTTP response (connection refused, DNS, timeout, abort). */
export class TransportError extends ScrupleError {
  override name = "TransportError";
  constructor(message: string, cause?: unknown) {
    super(message, { cause });
  }
}

/** The server answered with a non-2xx status. */
export class ApiError extends ScrupleError {
  override name = "ApiError";
  constructor(
    readonly status: number,
    readonly detail: unknown,
    readonly method: string,
    readonly path: string,
    readonly requestId: string | null,
  ) {
    super(`${method} ${path} -> ${status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
}

export class BadRequestError extends ApiError {
  override name = "BadRequestError";
}
/** 403: the actor may not do this (e.g. approving its own rule). */
export class ForbiddenError extends ApiError {
  override name = "ForbiddenError";
}
/** 404: unknown rule, version or decision. */
export class NotFoundError extends ApiError {
  override name = "NotFoundError";
}
/** 409: duplicate content or an invalid state transition. */
export class ConflictError extends ApiError {
  override name = "ConflictError";
}
/** 422: input validation failed; `detail` lists the problems. */
export class ValidationError extends ApiError {
  override name = "ValidationError";
}
export class RateLimitedError extends ApiError {
  override name = "RateLimitedError";
}
/** 5xx. */
export class ServerError extends ApiError {
  override name = "ServerError";
}

type ApiErrorClass = new (
  status: number,
  detail: unknown,
  method: string,
  path: string,
  requestId: string | null,
) => ApiError;

const BY_STATUS: Record<number, ApiErrorClass> = {
  400: BadRequestError,
  403: ForbiddenError,
  404: NotFoundError,
  409: ConflictError,
  422: ValidationError,
  429: RateLimitedError,
};

export function errorFor(status: number): ApiErrorClass {
  return BY_STATUS[status] ?? (status >= 500 ? ServerError : ApiError);
}
