"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { DataSourceKind } from "../../lib/api/data-source";
import { toApiError } from "../../lib/api/errors";
import type { ApiError } from "../../lib/api/errors";
import {
  HEALTH_TIMEOUT_CODE,
  HEALTH_TIMEOUT_MS,
  fetchHealth,
} from "../../lib/api/health";
import { formatOccurredAt } from "../../lib/attendance/format";
import { FIXTURE_LABEL } from "../../lib/fixtures/overview-fixtures";
import { Button } from "../ui/Button";
import styles from "./SystemStatusView.module.css";

interface HealthFailure {
  readonly title: string;
  readonly detail: string;
  readonly code: string;
  readonly status?: number;
}

type Check =
  | { readonly status: "loading" }
  | { readonly status: "healthy"; readonly checkedAt: string }
  | { readonly status: "failed"; readonly failure: HealthFailure };

/**
 * Turn a transport failure into operator-facing words.
 *
 * The classifications follow the `ApiError` kind the client already produces, so
 * a timeout, an unreachable backend, an unexpected payload and a server error are
 * told apart instead of collapsing into "failed". Only the sanitized code and
 * status are echoed; no stack trace, origin or upstream payload is rendered.
 */
function describeFailure(error: ApiError): HealthFailure {
  const reference = {
    code: error.code,
    ...(error.status === undefined ? {} : { status: error.status }),
  };

  if (error.kind === "network" && error.code === HEALTH_TIMEOUT_CODE) {
    return {
      title: "Health check timed out",
      detail: `The backend API did not answer within ${HEALTH_TIMEOUT_MS / 1000} seconds. A timeout means the check did not complete — it is not evidence that the service is down.`,
      ...reference,
    };
  }
  if (error.kind === "network") {
    return {
      title: "Backend API unreachable",
      detail:
        "The dashboard could not reach the backend API, so nothing is known about its current state. No retry was attempted automatically.",
      ...reference,
    };
  }
  if (error.kind === "http" && (error.status ?? 0) >= 500) {
    return {
      title: "Backend API returned a server error",
      detail: `The health request failed with a server error. Only the sanitized reference is shown. ${error.message}`,
      ...reference,
    };
  }
  if (error.kind === "http") {
    return {
      title: "Unexpected health response",
      detail: `The health request was answered with HTTP ${error.status ?? "error"} instead of a successful health payload, so this is not treated as healthy.`,
      ...reference,
    };
  }
  if (error.kind === "malformed") {
    return {
      title: "Unexpected health response",
      detail:
        'The backend API answered, but not with the documented { "status": "ok" } payload, so this is not treated as healthy.',
      ...reference,
    };
  }
  return {
    title: "Health check failed",
    detail:
      "The health check could not be completed, so no backend status is claimed.",
    ...reference,
  };
}

/**
 * System Status: one liveness check of the backend API.
 *
 * The screen deliberately claims no more than the endpoint does. A 200 with
 * `{ "status": "ok" }` means this API process answered; it is not a verdict on
 * PostgreSQL, cameras, recognition, enrolment or deployment, and the page lists
 * those gaps instead of inventing indicators for them.
 *
 * Fixture mode performs **no** request at all: synthetic data can never produce
 * a "healthy" backend result, and no health value is displayed for it.
 */
export function SystemStatusView({
  dataSourceKind,
}: {
  readonly dataSourceKind: DataSourceKind;
}) {
  const fixture = dataSourceKind === "fixture";
  const [check, setCheck] = useState<Check>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  // Initialised once at mount: the first attempt starts with this render, and
  // later attempts update it from the Refresh handler (never inside an effect).
  const [lastAttemptAt, setLastAttemptAt] = useState<string | null>(() =>
    fixture ? null : new Date().toISOString(),
  );
  const [lastSuccessAt, setLastSuccessAt] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  useEffect(() => {
    if (fixture) {
      // No request, no fabricated result: fixture mode never reports on
      // infrastructure it did not contact.
      return;
    }

    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;

    void (async () => {
      try {
        await fetchHealth(controller.signal);
        if (controller.signal.aborted || requestId !== requestIdRef.current) {
          return;
        }
        const checkedAt = new Date().toISOString();
        setLastSuccessAt(checkedAt);
        setCheck({ status: "healthy", checkedAt });
      } catch (error) {
        if (controller.signal.aborted || requestId !== requestIdRef.current) {
          return;
        }
        setCheck({ status: "failed", failure: describeFailure(toApiError(error)) });
      }
    })();

    return () => controller.abort();
  }, [attempt, fixture]);

  const reload = useCallback(() => {
    if (fixture) {
      return;
    }
    // A new attempt replaces the previous result: a stale "Healthy" is never
    // shown while the fresh check is running.
    setCheck({ status: "loading" });
    setLastAttemptAt(new Date().toISOString());
    setAttempt((current) => current + 1);
  }, [fixture]);

  const loading = !fixture && check.status === "loading";

  return (
    <section className={styles.page} aria-labelledby="page-title">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className={styles.title} id="page-title">
            System Status
          </h1>
          <p className={styles.lead}>
            This screen asks the backend API whether it is running, through the
            dashboard&apos;s same-origin proxy. It is a liveness check for that one
            service, not a health verdict for the whole attendance system.
          </p>
        </div>
        <Button variant="secondary" onClick={reload} disabled={fixture || loading}>
          {loading ? "Checking…" : "Refresh"}
        </Button>
      </header>

      {fixture ? (
        <div className={styles.fixture} role="note">
          <p className={styles.fixtureLabel}>{FIXTURE_LABEL}</p>
          <p className={styles.fixtureText}>
            Fixture mode is active, so live backend health has not been checked
            and no backend status is shown. Synthetic data never fabricates an
            infrastructure result; remove <code>DASHBOARD_DATA_MODE</code> to
            check the real service.
          </p>
        </div>
      ) : null}

      <section className={styles.card} aria-labelledby="backend-api-title">
        <div className={styles.cardHead}>
          <h2 className={styles.cardTitle} id="backend-api-title">
            Backend API
          </h2>
          <p className={styles.endpoint}>
            <code>GET /api/v1/health</code>
          </p>
        </div>

        {fixture ? (
          <p className={styles.stateLine}>
            <span className={styles.badge}>Not checked</span> Live health cannot
            be checked while fixture mode is active.
          </p>
        ) : check.status === "loading" ? (
          <p className={styles.stateLine} role="status">
            <span className={styles.badge}>Checking…</span> Waiting for the
            backend API to answer the health check.
          </p>
        ) : check.status === "healthy" ? (
          <p className={styles.stateLine} role="status">
            <span className={styles.badge}>Healthy</span> Backend API reachable —
            it answered the health check at {formatOccurredAt(check.checkedAt)}.
            This confirms the API process is serving requests; it does not confirm
            its database, cameras or recognition pipeline.
          </p>
        ) : (
          <div className={styles.failure} role="alert">
            <p className={styles.failureTitle}>{check.failure.title}</p>
            <p className={styles.failureText}>{check.failure.detail}</p>
            <p className={styles.failureCode}>
              Reference: <code>{check.failure.code}</code>
              {check.failure.status === undefined
                ? ""
                : ` (HTTP ${check.failure.status})`}
            </p>
            <p className={styles.failureText}>
              Nothing was retried automatically. Use Refresh to check once more.
            </p>
          </div>
        )}

        <dl className={styles.facts}>
          <div>
            <dt>Last attempted check</dt>
            <dd>
              {lastAttemptAt === null ? "Not yet" : formatOccurredAt(lastAttemptAt)}
            </dd>
          </div>
          <div>
            <dt>Last successful check</dt>
            <dd>
              {lastSuccessAt === null ? "Not yet" : formatOccurredAt(lastSuccessAt)}
            </dd>
          </div>
          <div>
            <dt>Timeout</dt>
            <dd>{HEALTH_TIMEOUT_MS / 1000} seconds per attempt</dd>
          </div>
        </dl>
      </section>

      <section className={styles.card} aria-labelledby="scope-title">
        <h2 className={styles.cardTitle} id="scope-title">
          Scope and limitations
        </h2>
        <p className={styles.scopeText}>
          A successful result means only that this API process answered. This
          screen does not independently verify:
        </p>
        <ul className={styles.scopeList}>
          <li>PostgreSQL availability, schema validity or migration state</li>
          <li>Camera availability or device access</li>
          <li>Face detection or recognition accuracy</li>
          <li>Biometric enrolment or embedding storage</li>
          <li>Authentication or authorization</li>
          <li>Production deployment readiness</li>
        </ul>
        <p className={styles.note}>
          The endpoint is deliberately minimal — it returns{" "}
          <code>{'{ "status": "ok" }'}</code> and makes a liveness claim about this
          API process only. <code>GET /api/v1/employees</code> is the honest
          database probe, and no other component is checked by any endpoint this
          dashboard may call.
        </p>
      </section>
    </section>
  );
}
