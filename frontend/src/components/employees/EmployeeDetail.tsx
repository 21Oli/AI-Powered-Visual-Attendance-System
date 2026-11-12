"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { toApiError } from "../../lib/api/errors";
import type { ApiError } from "../../lib/api/errors";
import { fetchEmployeeById, renameEmployee } from "../../lib/api/employees";
import type { Employee } from "../../lib/api/types";
import {
  mapEmployeeMutationError,
  type EmployeeFormErrors,
} from "../../lib/employees/errors";
import { formatCreatedAt, formatStatus } from "../../lib/employees/format";
import { describeFullNameProblem } from "../../lib/employees/validation";
import { Button } from "../ui/Button";
import { StatusMessage } from "../ui/StatusMessage";
import { TextField } from "../ui/TextField";
import styles from "./EmployeeDetail.module.css";

export interface EmployeeDetailProps {
  employeeId: string;
  onClose: () => void;
  onRenamed: (employee: Employee) => void;
  onRequestDeactivate: (employee: Employee) => void;
}

type DetailState =
  | { status: "loading" }
  | { status: "ready"; employee: Employee }
  | { status: "error"; error: ApiError };

/**
 * Detail panel for one employee, read from the detail endpoint.
 *
 * Only `full_name` is editable — the backend PATCH contract accepts nothing
 * else — and the returned (normalized) record replaces what is displayed.
 */
export function EmployeeDetail({
  employeeId,
  onClose,
  onRenamed,
  onRequestDeactivate,
}: EmployeeDetailProps) {
  const [state, setState] = useState<DetailState>({ status: "loading" });
  const [name, setName] = useState("");
  const [errors, setErrors] = useState<EmployeeFormErrors>({});
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const employee = await fetchEmployeeById(employeeId, controller.signal);
        if (controller.signal.aborted) {
          return;
        }
        setState({ status: "ready", employee });
        setName(employee.full_name);
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }
        setState({ status: "error", error: toApiError(error) });
      }
    })();
    return () => controller.abort();
  }, [employeeId]);

  const submitRename = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending || state.status !== "ready") {
      return;
    }

    const problem = describeFullNameProblem(name);
    if (problem !== null) {
      setErrors({ fullName: problem });
      nameRef.current?.focus();
      return;
    }

    setPending(true);
    setErrors({});
    setMessage(null);
    try {
      const updated = await renameEmployee(state.employee.employee_id, name);
      setState({ status: "ready", employee: updated });
      setName(updated.full_name);
      setMessage("Name updated.");
      onRenamed(updated);
    } catch (error) {
      const mapped = mapEmployeeMutationError(toApiError(error));
      setErrors(mapped);
      if (mapped.fullName !== undefined) {
        nameRef.current?.focus();
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <section className={styles.panel} aria-labelledby="employee-detail-title">
      <div className={styles.head}>
        <h2
          className={styles.title}
          id="employee-detail-title"
          ref={headingRef}
          tabIndex={-1}
        >
          Employee detail
        </h2>
        <Button size="sm" variant="secondary" onClick={onClose}>
          Close
        </Button>
      </div>

      {state.status === "loading" ? (
        <p className={styles.pending} role="status">
          Loading employee…
        </p>
      ) : null}

      {state.status === "error" ? (
        <StatusMessage tone="error">
          {mapEmployeeMutationError(state.error).form ??
            "The employee could not be loaded."}
        </StatusMessage>
      ) : null}

      {state.status === "ready" ? (
        <>
          <dl className={styles.facts}>
            <div>
              <dt>Employee ID</dt>
              <dd>
                <code>{state.employee.employee_id}</code>
              </dd>
            </div>
            <div>
              <dt>Full name</dt>
              <dd>{state.employee.full_name}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{formatStatus(state.employee.is_active)}</dd>
            </div>
            <div>
              <dt>Created (UTC)</dt>
              <dd>{formatCreatedAt(state.employee.created_at)}</dd>
            </div>
          </dl>

          {message === null ? null : (
            <StatusMessage tone="success">{message}</StatusMessage>
          )}

          <form className={styles.rename} onSubmit={submitRename} noValidate>
            <h3 className={styles.subtitle}>Rename</h3>
            {errors.form === undefined ? null : (
              <StatusMessage tone="error">{errors.form}</StatusMessage>
            )}
            <TextField
              id="employee-rename"
              label="Full name"
              value={name}
              onChange={setName}
              inputRef={nameRef}
              required
              disabled={pending}
              autoComplete="off"
              error={errors.fullName ?? null}
            />
            <div className={styles.actions}>
              <Button type="submit" variant="primary" disabled={pending}>
                {pending ? "Saving…" : "Save name"}
              </Button>
              <Button
                variant="danger"
                onClick={() => onRequestDeactivate(state.employee)}
                disabled={pending || !state.employee.is_active}
              >
                Deactivate employee
              </Button>
            </div>
            <p className={styles.note}>
              Only the name can be changed. Employee ID, status and creation date
              are managed by the service, and deactivation has no undo endpoint.
            </p>
          </form>
        </>
      ) : null}
    </section>
  );
}
