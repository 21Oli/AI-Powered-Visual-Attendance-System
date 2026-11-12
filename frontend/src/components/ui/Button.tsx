import type { ButtonHTMLAttributes, Ref } from "react";

import styles from "./Button.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** React 19 passes `ref` as a normal prop; declared so focus can be managed. */
  ref?: Ref<HTMLButtonElement>;
}

/**
 * Minimal design-system button.
 *
 * No `"use client"` directive on purpose: the primitive itself uses no hooks or
 * browser APIs, so it can render in either environment. Components that pass
 * event handlers are client components, as usual.
 */
export function Button({
  variant = "secondary",
  size = "md",
  className,
  type = "button",
  ref,
  ...rest
}: ButtonProps) {
  const classes = [
    styles.button,
    styles[variant] ?? "",
    styles[size] ?? "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  return <button ref={ref} type={type} className={classes} {...rest} />;
}
