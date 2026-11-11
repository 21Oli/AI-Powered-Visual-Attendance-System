import type { NavIconName } from "../../lib/navigation";

/**
 * Inline navigation icons.
 *
 * Hand-drawn SVG paths instead of an icon package: four icons do not justify a
 * dependency, and inline `currentColor` strokes let the tokens colour them.
 * Every icon is decorative (`aria-hidden`) because its link already has a label.
 */
export function NavIcon({
  name,
  className,
}: {
  name: NavIconName;
  className?: string;
}) {
  return (
    <svg
      className={className}
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {name === "overview" ? (
        <>
          <rect x="3" y="3" width="7.5" height="7.5" rx="1.5" />
          <rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5" />
          <rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5" />
          <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5" />
        </>
      ) : null}

      {name === "employees" ? (
        <>
          <circle cx="9" cy="8" r="3.2" />
          <path d="M3 20c0-3.1 2.7-5 6-5s6 1.9 6 5" />
          <path d="M16 5.2a3 3 0 0 1 0 5.6" />
          <path d="M17.6 20c0-2.3-.7-3.9-2-4.7" />
        </>
      ) : null}

      {name === "attendance" ? (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 7.5V12l3.2 1.9" />
        </>
      ) : null}

      {name === "system-status" ? (
        <>
          <path d="M3 12h3.8l2.4-5.6 3.6 11.2 2.4-5.6H21" />
        </>
      ) : null}
    </svg>
  );
}
