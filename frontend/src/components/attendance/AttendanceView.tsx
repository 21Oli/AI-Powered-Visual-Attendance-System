"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { createOverviewSource } from "../../lib/api/data-source";
import type { DataSourceKind } from "../../lib/api/data-source";
import { toApiError } from "../../lib/api/errors";
import type { ApiError } from "../../lib/api/errors";
import type {
  AttendanceEvent,
  AttendanceResponse,
  Employee,
} from "../../lib/api/types";
import {
  ALL_ATTENDANCE_FILTERS,
  filterAttendanceEvents,
} from "../../lib/attendance/filters";
import type { AttendanceFilters } from "../../lib/attendance/filters";
import {
  DEFAULT_ATTENDANCE_PAGE_SIZE,
  paginateAttendanceEvents,
} from "../../lib/attendance/pagination";
import {
  deriveSessionStates,
  foldLatestEvents,
} from "../../lib/attendance/journal";
import { journalUtcDays, summariseJournal } from "../../lib/attendance/summary";
import { FIXTURE_LABEL } from "../../lib/fixtures/overview-fixtures";
import { MetricCard } from "../dashboard/MetricCard";
import { Button } from "../ui/Button";
import { AttendanceFilters as FiltersPanel } from "./AttendanceFilters";
import { AttendancePagination } from "./AttendancePagination";
import { AttendanceTable } from "./AttendanceTable";
import type { DirectoryState } from "./AttendanceTable";
import styles from "./AttendanceView.module.css";

type Resource<T> =
  | { status: "loading" }
  | { status: "ready"; value: T }
  | { status: "error"; error: ApiError };

/** Stable empty journal so an unavailable read never allocates a new array. */
const NO_EVENTS: readonly AttendanceEvent[] = [];

/**
 * Read-only Attendance page.
 *
 * Both resources load concurrently and independently: a failed journal still
 * leaves a loaded directory (and vice versa), and neither failure is papered
 * over with fixture data. The page never issues an attendance mutation — there
 * is no checkout control here and no client call to the checkout endpoint.
 *
 * All filtering and paging happen over the loaded journal snapshot, because the
 * backend attendance endpoint accepts no query parameters.
 */
export function AttendanceView({
  dataSourceKind,
}: {
  dataSourceKind: DataSourceKind;
}) {
  const [attendance, setAttendance] = useState<Resource<AttendanceResponse>>({
    status: "loading",
  });
  const [employees, setEmployees] = useState<Resource<readonly Employee[]>>({
    status: "loading",
  });
  const [attempt, setAttempt] = useState(0);
  const [filters, setFilters] = useState<AttendanceFilters>(
    ALL_ATTENDANCE_FILTERS,
  );
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(
    DEFAULT_ATTENDANCE_PAGE_SIZE,
  );
  const requestIdRef = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    const source = createOverviewSource(dataSourceKind);

    void (async () => {
      const [employeeResult, attendanceResult] = await Promise.allSettled([
        source.loadEmployees(controller.signal),
        source.loadAttendance(controller.signal),
      ]);
      // A newer request (or an aborted one) must never write its result.
      if (controller.signal.aborted || requestId !== requestIdRef.current) {
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

    return () => controller.abort();
  }, [attempt, dataSourceKind]);

  const reload = useCallback(() => {
    setAttendance({ status: "loading" });
    setEmployees({ status: "loading" });
    setAttempt((current) => current + 1);
  }, []);

  const changeFilters = useCallback((next: AttendanceFilters) => {
    setFilters(next);
    setPage(1);
  }, []);

  const changePageSize = useCallback((size: number) => {
    setPageSize(size);
    setPage(1);
  }, []);

  const events =
    attendance.status === "ready" ? attendance.value.events : NO_EVENTS;

  const directoryState: DirectoryState =
    employees.status === "loading"
      ? "loading"
      : employees.status === "error"
        ? "unavailable"
        : "ready";
  const employeeList = employees.status === "ready" ? employees.value : null;
  const employeesById =
    employeeList === null
      ? null
      : new Map(employeeList.map((employee) => [employee.employee_id, employee]));

  const sessionStates = deriveSessionStates(foldLatestEvents(events));
  const summary = summariseJournal(events);
  const utcDays = journalUtcDays(events);
  const pageResult = paginateAttendanceEvents(
    filterAttendanceEvents(events, filters),
    page,
    pageSize,
  );

  const firstItem =
    pageResult.totalItems === 0
      ? 0
      : (pageResult.page - 1) * pageResult.pageSize + 1;
  const lastItem = Math.min(
    pageResult.page * pageResult.pageSize,
    pageResult.totalItems,
  );

  const metricState =
    attendance.status === "loading"
      ? "loading"
      : attendance.status === "error"
        ? "unavailable"
        : "ready";
  const hasJournalEvents = summary.totalEvents > 0;
  const noMatches = hasJournalEvents && pageResult.totalItems === 0;

  return (
    <section className={styles.page} aria-labelledby="page-title">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className={styles.title} id="page-title">
            Attendance
          </h1>
          <p className={styles.lead}>
            The attendance journal in append order, with employee names resolved
            from the register. Filtering and paging happen in this page; the
            service exposes no attendance query parameters. This view is
            read-only.
          </p>
        </div>
        <Button variant="secondary" onClick={reload}>
          Refresh
        </Button>
      </header>

      {dataSourceKind === "fixture" ? (
        <div className={styles.fixture} role="note">
          <p className={styles.fixtureLabel}>{FIXTURE_LABEL}</p>
          <p className={styles.fixtureText}>
            Every employee and event below is synthetic. No attendance record is
            real and nothing on this page can be changed.
          </p>
        </div>
      ) : null}

      {attendance.status === "ready" &&
      attendance.value.incomplete_trailing_record ? (
        <div className={styles.warning} role="status">
          <p className={styles.warningTitle}>Journal integrity warning</p>
          <p className={styles.warningText}>
            The journal ends with an incomplete trailing record, so the most
            recent event may be missing. Nothing is repaired, rewritten or
            truncated from this page.
          </p>
        </div>
      ) : null}

      <ul className={styles.metrics}>
        <MetricCard
          label="Journal events"
          value={attendance.status === "ready" ? String(summary.totalEvents) : null}
          detail="All events in the loaded journal"
          state={metricState}
        />
        <MetricCard
          label="Check-ins"
          value={attendance.status === "ready" ? String(summary.checkIns) : null}
          detail={`${summary.checkOuts} check-out${summary.checkOuts === 1 ? "" : "s"} in the same journal`}
          state={metricState}
        />
        <MetricCard
          label="Open sessions"
          value={
            attendance.status === "ready" ? String(summary.openSessions) : null
          }
          detail={`Latest event is a check-in, across ${summary.employeesWithEvents} employee${summary.employeesWithEvents === 1 ? "" : "s"} with events`}
          state={metricState}
        />
        <MetricCard
          label="Undated events"
          value={
            attendance.status === "ready" ? String(summary.undatedEvents) : null
          }
          detail="Excluded from session derivation; visible under All dates"
          state={metricState}
        />
      </ul>

      <FiltersPanel
        filters={filters}
        onChange={changeFilters}
        utcDays={utcDays}
        employees={employeeList}
        directoryState={directoryState}
        matchingCount={pageResult.totalItems}
        totalCount={summary.totalEvents}
      />

      {employees.status === "error" ? (
        <div className={styles.errorPanel} role="alert">
          <p className={styles.errorTitle}>Employee details are unavailable</p>
          <p className={styles.errorText}>{employees.error.message}</p>
          <p className={styles.errorCode}>
            Reference: <code>{employees.error.code}</code>
          </p>
          <p className={styles.errorText}>
            Events are shown with employee IDs only; no names are invented and
            employee-specific filtering is unavailable.
          </p>
          <Button variant="secondary" size="sm" onClick={reload}>
            Retry
          </Button>
        </div>
      ) : null}

      {attendance.status === "error" ? (
        <div className={styles.errorPanel} role="alert">
          <p className={styles.errorTitle}>Attendance is unavailable</p>
          <p className={styles.errorText}>{attendance.error.message}</p>
          <p className={styles.errorCode}>
            Reference: <code>{attendance.error.code}</code>
          </p>
          <Button variant="secondary" size="sm" onClick={reload}>
            Retry
          </Button>
        </div>
      ) : null}

      {attendance.status === "loading" ? (
        <p className={styles.pending} role="status">
          Loading attendance…
        </p>
      ) : null}

      {attendance.status === "ready" && !hasJournalEvents ? (
        <p className={styles.empty}>
          The attendance journal contains no events. Nothing has been checked in
          or out yet.
        </p>
      ) : null}

      {noMatches ? (
        <div className={styles.empty}>
          <p>No events match these filters.</p>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => changeFilters(ALL_ATTENDANCE_FILTERS)}
          >
            Reset filters
          </Button>
        </div>
      ) : null}

      {attendance.status === "ready" && pageResult.items.length > 0 ? (
        <AttendanceTable
          events={pageResult.items}
          employees={employeesById}
          directoryState={directoryState}
          sessionStates={sessionStates}
        />
      ) : null}

      {attendance.status === "ready" ? (
        <AttendancePagination
          page={pageResult.page}
          totalPages={pageResult.totalPages}
          pageSize={pageResult.pageSize}
          totalItems={pageResult.totalItems}
          firstItem={firstItem}
          lastItem={lastItem}
          onPageChange={setPage}
          onPageSizeChange={changePageSize}
        />
      ) : null}

      {attendance.status === "ready" &&
      (summary.undatedEvents > 0 || summary.unclassifiedEvents > 0) ? (
        <p className={styles.caveat}>
          {summary.undatedEvents} event
          {summary.undatedEvents === 1 ? "" : "s"} had an unreadable timestamp
          and {summary.unclassifiedEvents} had an unrecognised type. They are
          listed as unavailable or unknown, never dated or classified by guess,
          and they are not part of the session figures.
        </p>
      ) : null}

      <p className={styles.note}>
        History stays in journal append order and is never sorted by timestamp.
        Session state comes from each employee&apos;s latest valid event, with
        timestamp ties resolved by the later journal position. All timestamps are
        shown in UTC.
      </p>
    </section>
  );
}

