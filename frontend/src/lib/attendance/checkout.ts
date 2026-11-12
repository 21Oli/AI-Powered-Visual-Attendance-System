import type { Employee } from "../api/types";
import { isManageableEmployeeId } from "../employees/validation";
import type { CheckoutFailure } from "./errors";
import type { SessionState } from "./journal";

/**
 * Operator check-out eligibility and operator-facing copy.
 *
 * The backend stays authoritative for the check-out itself: this module only
 * decides whether the dashboard may *offer* the action, and turns the
 * classifier's outcome into wording an operator can act on. Nothing here sends
 * a request, retries, closes a session or invents an event.
 *
 * Eligibility is deliberately conservative: every precondition must hold, and a
 * "no" always carries a reason so the UI never shows a dead control.
 */

/**
 * The service's default check-out cooldown, shown for information only.
 *
 * The dashboard never gates on a browser-side timer and never assumes this
 * value: `CHECKOUT_COOLDOWN` from the service is the authority, and the copy
 * says the configured value may differ.
 */
export const CHECKOUT_COOLDOWN_SECONDS = 300;

export type CheckoutBlockReason =
  | "fixture"
  | "directory-loading"
  | "directory-unavailable"
  | "unresolved"
  | "inactive"
  | "unsupported-identifier"
  | "session-unknown"
  | "no-open-session"
  | "pending";

/** The single place each refusal is explained. */
export const CHECKOUT_EXPLANATIONS: Record<CheckoutBlockReason, string> = {
  fixture:
    "Operator check-out needs live backend data; these events are synthetic.",
  "directory-loading":
    "Employee details are still loading, so check-out cannot be authorised yet.",
  "directory-unavailable":
    "Employee details are unavailable, so check-out cannot be authorised.",
  unresolved: "This employee is not in the loaded register.",
  inactive: "Inactive employees cannot be checked out.",
  "unsupported-identifier":
    "This identifier cannot be addressed through the dashboard's API proxy.",
  "session-unknown":
    "The journal shows no reliable current session for this employee.",
  "no-open-session": "The journal shows no open session for this employee.",
  pending: "A check-out for this employee is already in progress.",
};

export type CheckoutEligibility =
  | { readonly eligible: true }
  | {
      readonly eligible: false;
      readonly reason: CheckoutBlockReason;
      readonly explanation: string;
    };

export interface CheckoutEligibilityInput {
  /** The resolved register record, or `null` when the id is not in it. */
  readonly employee: Employee | null;
  /** Derived current session state, or `undefined` when none is derivable. */
  readonly session: SessionState | undefined;
  readonly directoryState: "loading" | "ready" | "unavailable";
  /** `false` in fixture mode: synthetic data can never be checked out. */
  readonly liveData: boolean;
  /** A check-out for this employee is already in flight. */
  readonly pending: boolean;
}

function block(reason: CheckoutBlockReason): CheckoutEligibility {
  return {
    eligible: false,
    reason,
    explanation: CHECKOUT_EXPLANATIONS[reason],
  };
}

/**
 * Decide whether the operator may check this employee out.
 *
 * Order matters only for the explanation shown; every failed condition blocks.
 * A historical check-in row is never enough — the state must come from the
 * shared latest-event fold, and an unknown state blocks.
 */
export function checkoutEligibility(
  input: CheckoutEligibilityInput,
): CheckoutEligibility {
  if (!input.liveData) {
    return block("fixture");
  }
  if (input.directoryState === "loading") {
    return block("directory-loading");
  }
  if (input.directoryState !== "ready") {
    return block("directory-unavailable");
  }
  if (input.pending) {
    return block("pending");
  }
  if (input.employee === null) {
    return block("unresolved");
  }
  if (!isManageableEmployeeId(input.employee.employee_id)) {
    return block("unsupported-identifier");
  }
  if (!input.employee.is_active) {
    return block("inactive");
  }
  if (input.session === undefined) {
    return block("session-unknown");
  }
  if (input.session !== "open") {
    return block("no-open-session");
  }
  return { eligible: true };
}

export interface CheckoutFailureNotice {
  readonly title: string;
  readonly detail: string;
  /** The service's view may differ, so reload the journal and register. */
  readonly refresh: boolean;
  /** Offer an explicit journal refresh; the outcome may be recorded. */
  readonly verifyJournal: boolean;
}

/**
 * Operator-facing wording for a classified check-out failure.
 *
 * `persistence_unconfirmed` is the only uncertain outcome: it must never be
 * described as success or failure, and it never offers a retry as though the
 * previous request certainly wrote nothing.
 */
export function checkoutFailureNotice(
  failure: CheckoutFailure,
): CheckoutFailureNotice {
  switch (failure.kind) {
    case "employee_not_found":
      return {
        title: "Employee not found",
        detail:
          "The service no longer has this employee. The journal and register are being refreshed; this request recorded no check-out.",
        refresh: true,
        verifyJournal: false,
      };
    case "employee_inactive":
      return {
        title: "Employee is inactive",
        detail:
          "The service refused the check-out because the employee is inactive, so no event was written. The journal and register are being refreshed.",
        refresh: true,
        verifyJournal: false,
      };
    case "no_open_session":
      return {
        title: "No open session",
        detail:
          "The service found no open attendance session for this employee, so no event was written. The journal and register are being refreshed to their current state.",
        refresh: true,
        verifyJournal: false,
      };
    case "checkout_cooldown":
      return {
        title: "Check-out cooldown is still in effect",
        detail: `The service refused the check-out because one was already recorded inside its configured cooldown window (${CHECKOUT_COOLDOWN_SECONDS} seconds by default; the service's configuration is authoritative). This request wrote nothing.`,
        refresh: false,
        verifyJournal: false,
      };
    case "persistence_rejected":
      return {
        title: "Check-out was not recorded",
        detail:
          "The service rejected the check-out before persisting it. The journal is unchanged and the employee's session stays open.",
        refresh: false,
        verifyJournal: false,
      };
    case "persistence_unconfirmed":
      return {
        title: "Check-out outcome is uncertain",
        detail:
          "The service could not confirm whether it wrote the check-out, so the event may or may not be in the journal. It was not retried and nothing on this page marks the session closed. Refresh the journal and inspect this employee's latest event before taking any further action.",
        refresh: false,
        verifyJournal: true,
      };
    case "attendance_unavailable":
      return {
        title: "Attendance service unavailable",
        detail:
          "The service could not complete the check-out because the attendance journal is unavailable. Nothing here marks the session closed.",
        refresh: false,
        verifyJournal: false,
      };
    case "database_unavailable":
      return {
        title: "Employee register unavailable",
        detail:
          "The service could not complete the check-out because the employee register is unavailable. Nothing here marks the session closed.",
        refresh: false,
        verifyJournal: false,
      };
    default:
      return {
        title: "Check-out failed",
        detail:
          failure.message.trim().length > 0
            ? `${failure.message} No check-out is claimed.`
            : "The check-out did not complete. No check-out is claimed.",
        refresh: false,
        verifyJournal: false,
      };
  }
}
