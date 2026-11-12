import type { ApiError } from "../api/errors";

/**
 * Classification of a failed operator check-out.
 *
 * The backend publishes one refusal code with three reasons plus two distinct
 * persistence outcomes, and the difference matters to an operator: a business
 * refusal wrote nothing, a confirmed persistence rejection wrote nothing, and an
 * unconfirmed persistence failure may have written the event. This helper keeps
 * that distinction in one pure, testable place — it never retries, never
 * fabricates an event and never upgrades an uncertain outcome to success.
 */

export type CheckoutFailureKind =
  | "employee_not_found"
  | "employee_inactive"
  | "no_open_session"
  | "checkout_cooldown"
  | "persistence_rejected"
  | "persistence_unconfirmed"
  | "attendance_unavailable"
  | "database_unavailable"
  | "other";

export interface CheckoutFailure {
  readonly kind: CheckoutFailureKind;
  /** Fixed, UI-safe explanation for this classification. */
  readonly message: string;
  /** The original error code, preserved verbatim. */
  readonly code: string;
  readonly status?: number;
  /** The original `reason`, preserved verbatim (never synthesised). */
  readonly reason?: string;
  /**
   * `true` only for `persistence_unconfirmed`: the journal may or may not
   * contain the check-out. Callers must not claim success or failure, and must
   * not retry automatically.
   */
  readonly uncertain: boolean;
}

const MESSAGES: Record<Exclude<CheckoutFailureKind, "other">, string> = {
  employee_not_found: "That employee no longer exists.",
  employee_inactive: "This employee is inactive and cannot check out.",
  no_open_session: "The journal shows no open session for this employee.",
  checkout_cooldown:
    "A check-out was already recorded for this employee within the cooldown window.",
  persistence_rejected:
    "The check-out was rejected before any write; the journal is unchanged.",
  persistence_unconfirmed:
    "The check-out could not be confirmed as recorded. The journal may or may not contain it; it was not retried.",
  attendance_unavailable: "The attendance journal is unavailable.",
  database_unavailable: "The employee register is unavailable.",
};

/** The documented backend conditions, matched on code **and** status. */
function knownKind(error: ApiError): CheckoutFailureKind | null {
  if (error.code === "EMPLOYEE_NOT_FOUND" && error.status === 404) {
    return "employee_not_found";
  }
  if (error.code === "CHECKOUT_REJECTED" && error.status === 409) {
    // The reason is the only authority for which refusal occurred; a missing or
    // unrecognised reason stays "other" rather than being guessed.
    switch (error.reason) {
      case "EMPLOYEE_INACTIVE":
        return "employee_inactive";
      case "NO_OPEN_SESSION":
        return "no_open_session";
      case "CHECKOUT_COOLDOWN":
        return "checkout_cooldown";
      default:
        return null;
    }
  }
  if (error.code === "PERSISTENCE_REJECTED" && error.status === 422) {
    return "persistence_rejected";
  }
  if (error.code === "PERSISTENCE_UNCONFIRMED" && error.status === 503) {
    return "persistence_unconfirmed";
  }
  if (error.code === "ATTENDANCE_UNAVAILABLE" && error.status === 503) {
    return "attendance_unavailable";
  }
  if (error.code === "DATABASE_UNAVAILABLE" && error.status === 503) {
    return "database_unavailable";
  }
  return null;
}

/**
 * Classify a check-out failure.
 *
 * Pure and deterministic: the result depends only on the `ApiError`'s own
 * fields. Proxy failures, network failures, malformed payloads, 500s and
 * unknown codes all fall through to `other`, so transport problems are never
 * presented as business refusals.
 */
export function classifyCheckoutError(error: ApiError): CheckoutFailure {
  const kind = knownKind(error) ?? "other";
  return {
    kind,
    message: kind === "other" ? error.message : MESSAGES[kind],
    code: error.code,
    ...(error.status === undefined ? {} : { status: error.status }),
    ...(error.reason === undefined ? {} : { reason: error.reason }),
    uncertain: kind === "persistence_unconfirmed",
  };
}
