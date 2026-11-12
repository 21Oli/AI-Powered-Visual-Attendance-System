"use client";

import { useRef, useState } from "react";
import type { FormEvent } from "react";

import { createEmployee } from "../../lib/api/employees";
import { toApiError } from "../../lib/api/errors";
import type { Employee } from "../../lib/api/types";
import {
  mapCreateEmployeeError,
  type EmployeeFormErrors,
} from "../../lib/employees/errors";
import {
  MAX_EMPLOYEE_ID_LENGTH,
  describeEmployeeIdProblem,
  describeFullNameProblem,
} from "../../lib/employees/validation";
import { Button } from "../ui/Button";
import { StatusMessage } from "../ui/StatusMessage";
import { TextField } from "../ui/TextField";
import styles from "./CreateEmployeeForm.module.css";

export interface CreateEmployeeFormProps {
  onCreated: (employee: Employee) => void;
  onCancel: () => void;
}

/**
 * Create form.
 *
 * The identifier is validated against the proxy's rule **before** submitting,
 * so this form cannot create an employee the dashboard would then be unable to
 * address. The id is sent exactly as typed; the backend owns name normalization.
 */
export function CreateEmployeeForm({
  onCreated,
  onCancel,
}: CreateEmployeeFormProps) {
  const [employeeId, setEmployeeId] = useState("");
  const [fullName, setFullName] = useState("");
  const [errors, setErrors] = useState<EmployeeFormErrors>({});
  const [pending, setPending] = useState(false);
  const idRef = useRef<HTMLInputElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending) {
      return;
    }

    const idProblem = describeEmployeeIdProblem(employeeId);
    const nameProblem = describeFullNameProblem(fullName);
    if (idProblem !== null || nameProblem !== null) {
      setErrors({
        employeeId: idProblem ?? undefined,
        fullName: nameProblem ?? undefined,
      });
      if (idProblem !== null) {
        idRef.current?.focus();
      } else {
        nameRef.current?.focus();
      }
      return;
    }

    setPending(true);
    setErrors({});
    try {
      const created = await createEmployee({
        employee_id: employeeId,
        full_name: fullName,
      });
      onCreated(created);
    } catch (error) {
      const mapped = mapCreateEmployeeError(toApiError(error));
      setErrors(mapped);
      if (mapped.employeeId !== undefined) {
        idRef.current?.focus();
      } else if (mapped.fullName !== undefined) {
        nameRef.current?.focus();
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <h2 className={styles.title}>Create employee</h2>

      {errors.form === undefined ? null : (
        <StatusMessage tone="error">{errors.form}</StatusMessage>
      )}

      <TextField
        id="employee-id"
        label="Employee ID"
        value={employeeId}
        onChange={setEmployeeId}
        inputRef={idRef}
        required
        disabled={pending}
        maxLength={MAX_EMPLOYEE_ID_LENGTH}
        autoComplete="off"
        hint="Sent exactly as typed. It cannot contain / \ % # ? or spaces at either end, and cannot be '.' or '..'."
        error={errors.employeeId ?? null}
      />

      <TextField
        id="employee-full-name"
        label="Full name"
        value={fullName}
        onChange={setFullName}
        inputRef={nameRef}
        required
        disabled={pending}
        autoComplete="off"
        hint="The service trims and collapses repeated spaces before storing the name."
        error={errors.fullName ?? null}
      />

      <div className={styles.actions}>
        <Button type="submit" variant="primary" disabled={pending}>
          {pending ? "Creating…" : "Create employee"}
        </Button>
        <Button variant="secondary" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
