"use client";

import { useRef, useState } from "react";

import { checkoutEmployee } from "../../lib/api/attendance";
import type { DataSourceKind } from "../../lib/api/data-source";
import { toApiError } from "../../lib/api/errors";
import type { AttendanceEvent, Employee } from "../../lib/api/types";
import type { CheckoutFailureNotice } from "../../lib/attendance/checkout";
import {
  CHECKOUT_COOLDOWN_SECONDS,
  checkoutEligibility,
  checkoutFailureNotice,
} from "../../lib/attendance/checkout";
import { classifyCheckoutError } from "../../lib/attendance/errors";
import {
  UNKNOWN_EMPLOYEE_LABEL,
  formatOccurredAt,
  formatSessionState,
} from "../../lib/attendance/format";
import type { SessionState } from "../../lib/attendance/journal";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { StatusMessage } from "../ui/StatusMessage";
import type { DirectoryState } from "./AttendanceTable";
import styles from "./AttendanceCheckout.module.css";

type Outcome =
  | {
      readonly kind: "success";
      readonly employee: Employee;
      readonly event: AttendanceEvent;
    }
  | {
      readonly kind: "failure";
      readonly notice: CheckoutFailureNotice;
      readonly code: string;
      readonly status?: number;
      readonly reason?: string;
    }
  | { readonly kind: "blocked"; readonly explanation: string };

export interface AttendanceCheckoutProps {
  readonly dataSourceKind: DataSourceKind;
  /** Distinct employee ids in journal append order. */
  readonly employeeIds: readonly string[];
  readonly employees: ReadonlyMap<string, Employee> | null;
  readonly directoryState: DirectoryState;
  readonly sessionStates: ReadonlyMap<string, SessionState>;
  readonly journalLoading: boolean;
  /** Reload the journal and register from the service. */
  readonly onRefresh: () => void;
}

/**
 * Operator-authorized check-out for employees with an open session.
 *
 * The panel is a pure view of the derived session states: it offers the action
 * only when every precondition holds and states the reason otherwise. A
 * check-out is never optimistic — the journal is only reloaded after the service
 * confirms, and the session is closed only because the refreshed journal says so.
 *
 * One request is in flight at most. The pending employee's own control is
 * disabled, unrelated employees stay usable, and a second confirmation is
 * refused synchronously (not just by re-render) so double clicks cannot send
 * duplicate check-outs.
 */
export function AttendanceCheckout({
  dataSourceKind,
  employeeIds,
  employees,
  directoryState,
  sessionStates,
  journalLoading,
  onRefresh,
}: AttendanceCheckoutProps) {
  const [selected, setSelected] = useState<Employee | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const pendingRef = useRef<string | null>(null);

  const liveData = dataSourceKind === "live";
  const selectedEligibility =
    selected === null
      ? null
      : checkoutEligibility({
          employee: employees?.get(selected.employee_id) ?? selected,
          session: sessionStates.get(selected.employee_id),
          directoryState,
          liveData,
          pending: pendingId === selected.employee_id,
        });

  const confirm = async () => {
    const employee = selected;
    if (employee === null || pendingRef.current !== null) {
      return;
    }

    // Re-check against the current props: a refresh while the dialog was open
    // may have closed the session or dropped the employee from the register.
    const eligibility = checkoutEligibility({
      employee: employees?.get(employee.employee_id) ?? employee,
      session: sessionStates.get(employee.employee_id),
      directoryState,
      liveData,
      pending: false,
    });
    if (!eligibility.eligible) {
      setSelected(null);
      setOutcome({ kind: "blocked", explanation: eligibility.explanation });
      return;
    }

    pendingRef.current = employee.employee_id;
    setPendingId(employee.employee_id);
    setOutcome(null);

    try {
      // No abort signal on purpose: cancelling a write mid-flight would leave
      // the operator unsure whether the event landed, which is exactly the
      // uncertainty a confirmed check-out must not invent.
      const event = await checkoutEmployee(employee.employee_id);
      setOutcome({ kind: "success", employee, event });
      onRefresh();
    } catch (error) {
      const failure = classifyCheckoutError(toApiError(error));
      const notice = checkoutFailureNotice(failure);
      setOutcome({
        kind: "failure",
        notice,
        code: failure.code,
        ...(failure.status === undefined ? {} : { status: failure.status }),
        ...(failure.reason === undefined ? {} : { reason: failure.reason }),
      });
      if (notice.refresh) {
        onRefresh();
      }
    } finally {
      pendingRef.current = null;
      setPendingId(null);
      setSelected(null);
    }
  };

  const cancel = () => {
    if (pendingRef.current !== null) {
      return;
    }
    setSelected(null);
  };

  const selectedName =
    selected === null
      ? ""
      : (employees?.get(selected.employee_id)?.full_name ?? selected.full_name);

  return (
    <section className={styles.panel} aria-labelledby="operator-checkout-title">
      <div className={styles.head}>
        <h2 className={styles.title} id="operator-checkout-title">
          Operator checkout
        </h2>
        <p className={styles.lead}>
          An operator can check out an active employee with an open session.
          Confirming sends one operator-authorized request; the service records
          the event and enforces its own cooldown.
        </p>
      </div>

      {outcome === null ? null : outcome.kind === "success" ? (
        <StatusMessage tone="success">
          <strong>{outcome.employee.full_name}</strong> (
          {outcome.employee.employee_id}) was checked out at{" "}
          {formatOccurredAt(outcome.event.occurred_at)}. The journal has been
          reloaded from the service, so the session reads Closed only because the
          service recorded the event.
        </StatusMessage>
      ) : outcome.kind === "failure" ? (
        <>
          <StatusMessage tone="error">
            <strong>{outcome.notice.title}.</strong> {outcome.notice.detail}{" "}
            <span className={styles.reference}>
              Reference: {outcome.code}
              {outcome.status === undefined ? "" : ` (HTTP ${outcome.status})`}
              {outcome.reason === undefined ? "" : ` · ${outcome.reason}`}
            </span>
          </StatusMessage>
          {outcome.notice.verifyJournal ? (
            <div className={styles.actions}>
              <Button size="sm" onClick={onRefresh}>
                Refresh journal
              </Button>
              <span className={styles.hint}>
                Verify this employee&apos;s latest event before trying again; no
                retry is sent automatically.
              </span>
            </div>
          ) : null}
        </>
      ) : (
        <StatusMessage tone="error">
          <strong>Check-out is no longer available.</strong>{" "}
          {outcome.explanation} Nothing was sent.
        </StatusMessage>
      )}

      {!liveData ? (
        <p className={styles.note}>
          Operator checkout requires live backend data. These records are
          synthetic, so check-out is disabled here and no fixture record is ever
          changed.
        </p>
      ) : directoryState !== "ready" || employees === null ? (
        <p className={styles.note}>
          {directoryState === "loading"
            ? "Employee details are still loading, so check-out is unavailable until they arrive."
            : "Employee details are unavailable, so no check-out can be authorised. Employee IDs are shown without names and the action stays disabled."}
        </p>
      ) : journalLoading ? (
        <p className={styles.pending} role="status">
          Loading attendance…
        </p>
      ) : employeeIds.length === 0 ? (
        <p className={styles.note}>
          No employee has an event in the loaded journal, so there is nothing to
          check out.
        </p>
      ) : (
        <ul className={styles.rows}>
          {employeeIds.map((employeeId) => {
            const employee = employees.get(employeeId) ?? null;
            const isPending = pendingId === employeeId;
            const eligibility = checkoutEligibility({
              employee,
              session: sessionStates.get(employeeId),
              directoryState,
              liveData,
              pending: isPending,
            });
            const name = employee?.full_name ?? UNKNOWN_EMPLOYEE_LABEL;

            return (
              <li className={styles.row} key={employeeId}>
                <div className={styles.who}>
                  <span className={styles.name}>{name}</span>
                  <code className={styles.id}>{employeeId}</code>
                  <span className={styles.state}>
                    {formatSessionState(sessionStates.get(employeeId))}
                  </span>
                </div>
                <div className={styles.action}>
                  {isPending ? (
                    <Button
                      size="sm"
                      variant="primary"
                      disabled
                      aria-busy="true"
                      aria-label={`Check out ${name}`}
                    >
                      Checking out…
                    </Button>
                  ) : employee !== null && eligibility.eligible ? (
                    <Button
                      size="sm"
                      variant="primary"
                      aria-label={`Check out ${name}`}
                      onClick={() => setSelected(employee)}
                    >
                      Check out
                    </Button>
                  ) : (
                    <span className={styles.reason}>
                      {eligibility.eligible
                        ? "Check-out is unavailable for this employee."
                        : eligibility.explanation}
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {pendingId === null ? null : (
        <p className={styles.pending} role="status">
          Check-out pending for {pendingId}. No duplicate request is sent while it
          is in flight.
        </p>
      )}

      <ConfirmDialog
        open={selected !== null}
        title={
          selected === null
            ? "Check out employee?"
            : `Check out ${selectedName}?`
        }
        description={
          selected === null ? null : (
            <>
              <p>
                <strong>{selectedName}</strong> ·{" "}
                <code>{selected.employee_id}</code>
              </p>
              <p>
                This is an <strong>operator-authorized</strong> check-out.
                Confirming records a check-out attendance event for this employee
                in the journal.
              </p>
              <p>
                The service applies its configured check-out cooldown (
                {CHECKOUT_COOLDOWN_SECONDS} seconds by default, but its
                configuration is authoritative) and refuses a second check-out
                inside that window.
              </p>
              <p>
                The dashboard cannot undo a check-out: reversing it would need a
                new attendance record, so confirm only for the employee you mean
                to check out.
              </p>
              {selectedEligibility === null ||
              selectedEligibility.eligible ? null : (
                <p>
                  Check-out is currently unavailable:{" "}
                  {selectedEligibility.explanation}
                </p>
              )}
            </>
          )
        }
        confirmLabel="Check out"
        pending={pendingId !== null}
        onConfirm={() => void confirm()}
        onCancel={cancel}
      />
    </section>
  );
}
