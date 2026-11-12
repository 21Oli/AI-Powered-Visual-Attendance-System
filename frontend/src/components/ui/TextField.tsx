import type { RefObject } from "react";

import styles from "./TextField.module.css";

export interface TextFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  error?: string | null;
  required?: boolean;
  disabled?: boolean;
  maxLength?: number;
  autoComplete?: string;
  inputRef?: RefObject<HTMLInputElement | null>;
}

/**
 * Labelled text input with hint and field-level error.
 *
 * The label is a real `<label for>`, the hint and error are referenced through
 * `aria-describedby`, and an error also sets `aria-invalid` — so the message is
 * announced with the field rather than floating beside it.
 */
export function TextField({
  id,
  label,
  value,
  onChange,
  hint,
  error,
  required = false,
  disabled = false,
  maxLength,
  autoComplete,
  inputRef,
}: TextFieldProps) {
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy =
    [hint === undefined ? null : hintId, error == null ? null : errorId]
      .filter((entry): entry is string => entry !== null)
      .join(" ") || undefined;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
        {required ? (
          <span className={styles.required} aria-hidden="true">
            {" "}
            *
          </span>
        ) : null}
      </label>

      {hint === undefined ? null : (
        <p className={styles.hint} id={hintId}>
          {hint}
        </p>
      )}

      <input
        ref={inputRef}
        id={id}
        name={id}
        type="text"
        className={error == null ? styles.input : `${styles.input} ${styles.inputInvalid}`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={error == null ? undefined : true}
        aria-describedby={describedBy}
        aria-required={required ? true : undefined}
        disabled={disabled}
        maxLength={maxLength}
        autoComplete={autoComplete}
      />

      {error == null ? null : (
        <p className={styles.error} id={errorId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
