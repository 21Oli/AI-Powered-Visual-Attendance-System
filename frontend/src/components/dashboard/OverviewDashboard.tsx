"use client";

import { useCallback, useEffect, useState } from "react";

import { createOverviewSource } from "../../lib/api/data-source";
import type { DataSourceKind } from "../../lib/api/data-source";
import { toApiError } from "../../lib/api/errors";
import type { ApiError } from "../../lib/api/errors";
import type { AttendanceResponse, Employee } from "../../lib/api/types";
import {
  countEventsOnUtcDay,
  summariseEmployees,
  summariseSessions,
} from "../../lib/dashboard/overview";
import { FIXTURE_LABEL } from "../../lib/fixtures/overview-fixtures";
import { Button } from "../ui/Button";
import { MetricCard } from "./MetricCard";
import styles from "./OverviewDashboard.module.css";

type SectionState<T> =
  | { readonly status: "loading" }
  | { readonly status: "ready"; readonly value: T }
  | { readonly status: "error"; readonly error: ApiError };

function SectionError({
  title,
  error,
  onRetry,
}: {
  title: string;
  error: ApiError;
  onRetry: () => void;
}) {
  return (
    <div className={styles.error} role="alert">
      <p className={styles.errorTitle}>{title}</p>
      <p className={styles.errorText}>{error.message}</p>
      <p className={styles.errorCode}>
        Reference: <code>{error.code}</code>
      </p>
      <Button variant="secondary" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/**
 * Overview: four figures derived only from the two read-only endpoints.
 *
 * Employees and attendance load independently, so one failing section never
 * hides the other. Every value is computed from a validated response — there is
 * no rate, trend, score or estimate anywhere, and a failed section shows
 * "Unavailable" rather than a number.
 */
export function OverviewDashboard({
  dataSourceKind,
}: {
  dataSourceKind: DataSourceKind;
}) {
  const [employees, setEmployees] = useState<SectionState<Employee[]>>({
    status: "loading",
  });
  const [attendance, setAttendance] = useState<SectionState<AttendanceResponse>>({
    status: "loading",
  });
  const [attempt, setAttempt] = useState(0);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const reload = useCallback(() => {
    setEmployees({ status: "loading" });
    setAttendance({ status: "loading" });
    setNowMs(Date.now());
    setAttempt((current) => current + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const source = createOverviewSource(dataSourceKind);

    void (async () => {
      const [employeeResult, attendanceResult] = await Promise.allSettled([
        source.loadEmployees(controller.signal),
        source.loadAttendance(controller.signal),
      ]);
      if (!active) {
        return;
      }
      setEmployees(
        employeeResult.status === "fulfilled"
          ? { status: "ready", value: employeeResult.value }
          : { status: "error", error: toApiError(employeeResult.reason) },
      );
      setAttendance(
        attendanceResult.status === "fulfilled"
          ? { status: "ready", value: attendanceResult.value }
          : { status: "error", error: toApiError(attendanceResult.reason) },
      );
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt, dataSourceKind]);

  const totals =
    employees.status === "ready" ? summariseEmployees(employees.value) : null;
  const journal = attendance.status === "ready" ? attendance.value : null;
  const sessions = journal === null ? null : summariseSessions(journal.events);
  const eventsToday =
    journal === null ? null : countEventsOnUtcDay(journal.events, nowMs);

  const loading = employees.status === "loading" || attendance.status === "loading";

  const employeesState =
    employees.status === "loading"
      ? "loading"
      : employees.status === "error"
        ? "unavailable"
        : "ready";
  const attendanceState =
    attendance.status === "loading"
      ? "loading"
      : attendance.status === "error"
        ? "unavailable"
        : "ready";

  return (
    <section className={styles.page} aria-labelledby="page-title">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 id="page-title" className={styles.title}>
            Overview
          </h1>
          <p className={styles.lead}>
            Employee totals, today&apos;s attendance activity and open sessions,
            read straight from the attendance service. No figures are estimated.
          </p>
        </div>
        <Button variant="secondary" onClick={reload}>
          Refresh
        </Button>
      </header>

      {dataSourceKind === "fixture" ? (
        <p className={styles.fixture} role="note">
          {FIXTURE_LABEL}
        </p>
      ) : null}

      <p className={styles.srOnly} role="status" aria-live="polite">
        {loading ? "Loading dashboard data." : "Dashboard data loaded."}
      </p>

      {journal !== null && journal.incomplete_trailing_record ? (
        <div className={styles.warning} role="status">
          <p className={styles.warningTitle}>Journal integrity warning</p>
          <p className={styles.warningText}>
            The attendance journal ends with an incomplete trailing record, so
            these figures may omit the most recent event. The journal is never
            repaired or rewritten from the dashboard.
          </p>
        </div>
      ) : null}

      <ul className={styles.metrics}>
        <MetricCard
          label="Total employees"
          value={totals === null ? null : String(totals.total)}
          detail={
            totals === null
              ? undefined
              : totals.total === 0
                ? "No employees are registered yet."
                : `${totals.inactive} inactive`
          }
          state={employeesState}
        />
        <MetricCard
          label="Active employees"
          value={totals === null ? null : String(totals.active)}
          detail={totals === null ? undefined : "is_active = true"}
          state={employeesState}
        />
        <MetricCard
          label="Events today (UTC)"
          value={eventsToday === null ? null : String(eventsToday)}
          detail={`UTC day ${new Date(nowMs).toISOString().slice(0, 10)}`}
          state={attendanceState}
        />
        <MetricCard
          label="Open sessions"
          value={sessions === null ? null : String(sessions.openSessions)}
          detail={
            sessions === null
              ? undefined
              : `Latest event is a check-in, across ${sessions.employeesWithEvents} employee${
                  sessions.employeesWithEvents === 1 ? "" : "s"
                } with events`
          }
          state={attendanceState}
        />
      </ul>

      {employees.status === "error" ? (
        <SectionError
          title="Employee data is unavailable"
          error={employees.error}
          onRetry={reload}
        />
      ) : null}

      {attendance.status === "error" ? (
        <SectionError
          title="Attendance data is unavailable"
          error={attendance.error}
          onRetry={reload}
        />
      ) : null}

      {sessions !== null &&
      (sessions.undatedEvents > 0 || sessions.unclassifiedEvents > 0) ? (
        <p className={styles.caveat}>
          {sessions.undatedEvents} event
          {sessions.undatedEvents === 1 ? "" : "s"} had an unreadable timestamp
          and {sessions.unclassifiedEvents} had an unrecognised type; they were
          left out of the session count.
        </p>
      ) : null}

      <p className={styles.note}>
        Totals are counted across every employee page; open sessions are folded
        from the latest event per employee. When a section cannot be read it
        says so instead of showing a value.
      </p>
    </section>
  );
}
