/**
 * Claimant-facing API client for the EI-ADS portal.
 *
 * All calls use the HttpOnly GCKey session cookie (`credentials: 'include'`),
 * matching the auth driver in {@link ../auth/gckey}. The SPA never handles
 * tokens directly; the FastAPI portal backend authenticates each request and
 * enforces that a CLAIMANT may only read/act on their own records
 * (Req 19.7, 22.6). The backend endpoints themselves are implemented by
 * task 28.1; this module is the thin client the status view calls.
 */

import type { SupportedLanguage } from '../i18n/config';

/**
 * Routing outcome assigned to a claim (design § Risk Score / Risk Decision
 * Model, Glossary "Risk_Decision"). INDETERMINATE is emitted when the
 * pipeline could not run the minimum number of engines.
 */
export type RiskDecision =
  | 'AUTO_APPROVE'
  | 'FLAG_FOR_REVIEW'
  | 'INVESTIGATE'
  | 'INDETERMINATE';

export const RISK_DECISIONS: readonly RiskDecision[] = [
  'AUTO_APPROVE',
  'FLAG_FOR_REVIEW',
  'INVESTIGATE',
  'INDETERMINATE',
];

/** Lifecycle status of a claim as surfaced to the claimant. */
export type ClaimStatus =
  | 'RECEIVED'
  | 'PROCESSING'
  | 'DECIDED'
  | 'UNDER_REVIEW'
  | 'CLOSED';

export const CLAIM_STATUSES: readonly ClaimStatus[] = [
  'RECEIVED',
  'PROCESSING',
  'DECIDED',
  'UNDER_REVIEW',
  'CLOSED',
];

/**
 * Claim status payload returned by `GET /portal/claims/{claim_id}`.
 *
 * The bilingual LLM explanation is delivered as parallel EN/FR text
 * (Req 20.2); the view selects the copy matching the active UI language.
 * `riskDecision`/`riskScore` may be null while the claim is still processing.
 */
export interface ClaimStatusView {
  claimId: string;
  status: ClaimStatus;
  riskDecision: RiskDecision | null;
  riskScore: number | null;
  explanationEn: string | null;
  explanationFr: string | null;
  submittedAt: string | null;
  decidedAt: string | null;
}

/** Error thrown when a portal API call returns a non-OK HTTP status. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function isRiskDecision(value: unknown): value is RiskDecision {
  return (
    typeof value === 'string' &&
    (RISK_DECISIONS as readonly string[]).includes(value)
  );
}

function isClaimStatus(value: unknown): value is ClaimStatus {
  return (
    typeof value === 'string' &&
    (CLAIM_STATUSES as readonly string[]).includes(value)
  );
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Parse a raw claim-status payload defensively. Returns null when the payload
 * is not a well-formed object with a claim id and a recognised status, so the
 * view can render a "not found / unavailable" state rather than crash.
 */
export function parseClaimStatus(payload: unknown): ClaimStatusView | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const claimId = record.claim_id ?? record.claimId;
  const status = record.status;
  if (typeof claimId !== 'string' || claimId.length === 0) {
    return null;
  }
  if (!isClaimStatus(status)) {
    return null;
  }
  const rawDecision = record.risk_decision ?? record.riskDecision;
  return {
    claimId,
    status,
    riskDecision: isRiskDecision(rawDecision) ? rawDecision : null,
    riskScore: optionalNumber(record.risk_score ?? record.riskScore),
    explanationEn: optionalString(record.explanation_en ?? record.explanationEn),
    explanationFr: optionalString(record.explanation_fr ?? record.explanationFr),
    submittedAt: optionalString(record.submitted_at ?? record.submittedAt),
    decidedAt: optionalString(record.decided_at ?? record.decidedAt),
  };
}

/**
 * Select the LLM explanation matching the active UI language (Req 20.2),
 * falling back to the other official language if the preferred one is absent.
 */
export function selectExplanation(
  view: Pick<ClaimStatusView, 'explanationEn' | 'explanationFr'>,
  language: SupportedLanguage,
): string | null {
  const preferred = language === 'fr' ? view.explanationFr : view.explanationEn;
  const fallback = language === 'fr' ? view.explanationEn : view.explanationFr;
  return preferred ?? fallback ?? null;
}

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.clone().json();
    if (
      body != null &&
      typeof body === 'object' &&
      typeof (body as Record<string, unknown>).message === 'string'
    ) {
      return (body as Record<string, string>).message;
    }
  } catch {
    // Ignore malformed/non-JSON error bodies; fall back to the status text.
  }
  return response.statusText || `HTTP ${response.status}`;
}

/**
 * Fetch the status of a single claim for the authenticated claimant.
 * @throws ApiError when the backend responds with a non-OK status.
 */
export async function fetchClaimStatus(
  claimId: string,
): Promise<ClaimStatusView | null> {
  const response = await fetch(
    `/portal/claims/${encodeURIComponent(claimId)}`,
    {
      method: 'GET',
      credentials: 'include',
      headers: { Accept: 'application/json' },
    },
  );

  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readErrorMessage(response));
  }

  const payload: unknown = await response.json();
  return parseClaimStatus(payload);
}

/** Outcome of a Privacy Act request (access or deletion). */
export interface PrivacyRequestResult {
  /** Backend-assigned reference the claimant can quote when following up. */
  referenceId: string | null;
  /** ISO-8601 timestamp by which the request will be fulfilled (30-day SLA). */
  fulfilBy: string | null;
}

function parsePrivacyResult(payload: unknown): PrivacyRequestResult {
  if (payload == null || typeof payload !== 'object') {
    return { referenceId: null, fulfilBy: null };
  }
  const record = payload as Record<string, unknown>;
  return {
    referenceId: optionalString(record.reference_id ?? record.referenceId),
    fulfilBy: optionalString(record.fulfil_by ?? record.fulfilBy),
  };
}

/**
 * Submit a Privacy Act data access request (Req 19.4). The backend returns all
 * records held for the authenticated claimant's SIN in a machine-readable
 * format within the 30-day statutory SLA.
 */
export async function requestDataAccess(): Promise<PrivacyRequestResult> {
  const response = await fetch('/api/v1/privacy/access', {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorMessage(response));
  }
  return parsePrivacyResult(await response.json());
}

/**
 * Submit a Privacy Act data deletion / erasure request (Req 19.5). The backend
 * anonymises identifying fields for the claimant's SIN while preserving the
 * aggregate records required for audit. Callers MUST confirm intent with the
 * user before invoking this irreversible action.
 */
export async function requestDataDeletion(): Promise<PrivacyRequestResult> {
  const response = await fetch('/api/v1/privacy/delete', {
    method: 'DELETE',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorMessage(response));
  }
  return parsePrivacyResult(await response.json());
}
