"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { toApiError } from "../../lib/api/errors";
import type { ApiError } from "../../lib/api/errors";
import { deactivateEmployee, fetchEmployeePage } from "../../lib/api/employees";
import type { Employee, EmployeeListResponse } from "../../lib/api/types";
import { mapEmployeeMutationError } from "../../lib/employees/errors";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { StatusMessage } from "../ui/StatusMessage";
import { CreateEmployeeForm } from "./CreateEmployeeForm";
import { EmployeeDetail } from "./EmployeeDetail";
import { EmployeeTable } from "./EmployeeTable";
import { PaginationControls } from "./PaginationControls";
import styles from "./EmployeesView.module.css";

type ListState =
  | { status: "loading" }
  | { status: "ready"; page: EmployeeListResponse }
  | { status: "error"; error: ApiError };

const INITIAL_PAGE_SIZE = 20;

/**
 * Employees directory.
 *
 * Keyset only: the cursor stack records the `after` value that started each
 * page, so "Previous" is a local look-up and never a re-scan. Requests are
 * tagged with a monotonically increasing id and aborted on change, so a slow
 * response can never overwrite a newer page.
 */
export function EmployeesView() {
  const [limit, setLimit] = useState<number>(INITIAL_PAGE_SIZE);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [list, setList] = useState<ListState>({ status: "loading" });
  const [pending, setPending] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<
    { tone: "success" | "error"; message: string } | null
  >(null);
  const [deactivateTarget, setDeactivateTarget] = useState<Employee | null>(null);
  const [deactivatePending, setDeactivatePending] = useState(false);
  const [deactivateError, setDeactivateError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    const after = cursors[pageIndex] ?? null;

    void (async () => {
      try {
        const page = await fetchEmployeePage({
          after,
          limit,
          signal: controller.signal,
        });
        if (requestId !== requestIdRef.current) {
          return;
        }
        setList({ status: "ready", page });
      } catch (error) {
        if (controller.signal.aborted || requestId !== requestIdRef.current) {
          return;
        }
        setList({ status: "error", error: toApiError(error) });
      } finally {
        if (requestId === requestIdRef.current) {
          setPending(false);
        }
      }
    })();

    return () => controller.abort();
  }, [cursors, pageIndex, limit, reloadToken]);

  const refresh = useCallback(() => {
    setPending(true);
    setReloadToken((token) => token + 1);
  }, []);

  const goToFirstPageAndRefresh = useCallback(() => {
    setPending(true);
    setCursors([null]);
    setPageIndex(0);
    setReloadToken((token) => token + 1);
  }, []);

  const goNext = useCallback(() => {
    if (list.status !== "ready" || list.page.next_after === null) {
      return;
    }
    const nextCursor = list.page.next_after;
    setPending(true);
    setCursors((previous) => {
      const next = previous.slice(0, pageIndex + 1);
      next[pageIndex + 1] = nextCursor;
      return next;
    });
    setPageIndex(pageIndex + 1);
  }, [list, pageIndex]);

  const goPrevious = useCallback(() => {
    if (pageIndex === 0) {
      return;
    }
    setPending(true);
    setPageIndex(pageIndex - 1);
  }, [pageIndex]);

  const changePageSize = useCallback((size: number) => {
    setPending(true);
    setLimit(size);
    setCursors([null]);
    setPageIndex(0);
  }, []);

  const handleCreated = useCallback((employee: Employee) => {
    setShowCreate(false);
    setNotice({
      tone: "success",
      message: `Employee ${employee.employee_id} was created. The directory is ordered by employee ID; page through it to find the record.`,
    });
    setPending(true);
    setCursors([null]);
    setPageIndex(0);
    setReloadToken((token) => token + 1);
  }, []);

  const requestDeactivate = useCallback((employee: Employee) => {
    setDeactivateError(null);
    setDeactivateTarget(employee);
  }, []);

  const cancelDeactivate = useCallback(() => {
    if (deactivatePending) {
      return;
    }
    setDeactivateTarget(null);
    setDeactivateError(null);
  }, [deactivatePending]);

  const confirmDeactivate = useCallback(async () => {
    const target = deactivateTarget;
    if (target === null || deactivatePending) {
      return;
    }
    setDeactivatePending(true);
    setDeactivateError(null);
    try {
      await deactivateEmployee(target.employee_id);
      setDeactivateTarget(null);
      setNotice({
        tone: "success",
        message: `Employee ${target.employee_id} was deactivated. The service has no reactivation endpoint.`,
      });
      if (selectedId === target.employee_id) {
        setSelectedId(null);
      }
      refresh();
    } catch (error) {
      const mapped = mapEmployeeMutationError(toApiError(error));
      setDeactivateError(
        mapped.form ?? "The employee could not be deactivated.",
      );
    } finally {
      setDeactivatePending(false);
    }
  }, [deactivateTarget, deactivatePending, refresh, selectedId]);

  const employees = list.status === "ready" ? list.page.employees : [];
  const isFirstPage = pageIndex === 0;
  const isEmptyDirectory = list.status === "ready" && list.page.employees.length === 0;

  return (
    <section className={styles.page} aria-labelledby="page-title">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className={styles.title} id="page-title">
            Employees
          </h1>
          <p className={styles.lead}>
            Create employees, correct a name and deactivate a record. The
            directory is ordered by employee ID and read from the attendance
            service.
          </p>
        </div>
        <div className={styles.headActions}>
          <Button
            variant="primary"
            onClick={() => setShowCreate((open) => !open)}
            aria-expanded={showCreate}
            aria-controls="create-employee-panel"
          >
            {showCreate ? "Close create form" : "Create employee"}
          </Button>
          <Button variant="secondary" onClick={refresh} disabled={pending}>
            Refresh
          </Button>
        </div>
      </header>

      {notice === null ? null : (
        <StatusMessage
          tone={notice.tone}
          onDismiss={() => setNotice(null)}
        >
          {notice.message}
        </StatusMessage>
      )}

      {showCreate ? (
        <div id="create-employee-panel">
          <CreateEmployeeForm
            onCreated={handleCreated}
            onCancel={() => setShowCreate(false)}
          />
        </div>
      ) : null}

      {selectedId === null ? null : (
        <EmployeeDetail
          employeeId={selectedId}
          onClose={() => setSelectedId(null)}
          onRenamed={(employee) => {
            setNotice({
              tone: "success",
              message: `Employee ${employee.employee_id} was renamed.`,
            });
            refresh();
          }}
          onRequestDeactivate={requestDeactivate}
        />
      )}

      <section className={styles.directory} aria-labelledby="directory-title">
        <div className={styles.directoryHead}>
          <h2 className={styles.directoryTitle} id="directory-title">
            Directory
          </h2>
          <p className={styles.meta} aria-live="polite">
            {list.status === "ready"
              ? `Page ${pageIndex + 1} · ${employees.length} on this page${pending ? " · updating…" : ""}`
              : "Loading…"}
          </p>
        </div>

        {list.status === "loading" ? (
          <p className={styles.pending} role="status">
            Loading employees…
          </p>
        ) : null}

        {list.status === "error" ? (
          <div className={styles.errorPanel} role="alert">
            <p className={styles.errorTitle}>The employee directory is unavailable</p>
            <p className={styles.errorText}>{list.error.message}</p>
            <p className={styles.errorCode}>
              Reference: <code>{list.error.code}</code>
            </p>
            <Button variant="secondary" size="sm" onClick={refresh}>
              Retry
            </Button>
          </div>
        ) : null}

        {isEmptyDirectory && isFirstPage ? (
          <p className={styles.empty}>
            No employees are registered yet. Create the first employee to get
            started.
          </p>
        ) : null}

        {isEmptyDirectory && !isFirstPage ? (
          <div className={styles.empty}>
            <p>
              This page is empty. The previous page was full, so the service
              offered one more cursor, but the keyset walk ends here.
            </p>
            <Button variant="secondary" size="sm" onClick={goPrevious}>
              Back to the previous page
            </Button>
          </div>
        ) : null}

        {employees.length > 0 ? (
          <EmployeeTable
            employees={employees}
            onView={(employee) => setSelectedId(employee.employee_id)}
            onDeactivate={requestDeactivate}
          />
        ) : null}

        <PaginationControls
          pageIndex={pageIndex}
          pageSize={limit}
          canGoNext={list.status === "ready" && list.page.next_after !== null}
          canGoPrevious={!isFirstPage}
          pending={pending}
          onNext={goNext}
          onPrevious={goPrevious}
          onPageSizeChange={changePageSize}
        />

        <p className={styles.note}>
          Only paging is supported: the API has no total, no search and no
          sorting. Counts describe this page only.
        </p>
      </section>

      <ConfirmDialog
        open={deactivateTarget !== null}
        title="Deactivate employee"
        confirmLabel="Deactivate"
        pending={deactivatePending}
        error={deactivateError}
        onCancel={cancelDeactivate}
        onConfirm={() => {
          void confirmDeactivate();
        }}
        description={
          deactivateTarget === null ? null : (
            <>
              <p>
                Deactivate{" "}
                <strong>
                  {deactivateTarget.full_name} ({deactivateTarget.employee_id})
                </strong>
                ?
              </p>
              <p>
                Deactivation prevents future active-employee operations for this
                record. The service has no reactivation endpoint, so this cannot
                be undone through the dashboard.
              </p>
            </>
          )
        }
      />
    </section>
  );
}
