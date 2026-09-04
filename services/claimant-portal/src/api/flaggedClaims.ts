import type { Role } from '../auth/roles';
import { maskFlaggedClaimForRoles } from './pii';

/**
 * Investigator dashboard data access for the flagged-claims queue (Req 22.1).
 *
 * The backend exposes the queue at `GET /portal/investigator/claims`, returning
 * every claim whose Risk_Decision is FLAG_FOR_REVIEW or INVESTIGATE. The SPA
 * additionally sorts by Risk_Score descending as a defensive measure so the
 * ordering contract holds regardless of backend ordering (design § RBAC Role
 * Model, Req 22.1).
 *
 * PII masking (Req 22.6) is enforced authoritatively by the backend based on
 * the caller's role. The client applies the same masking rules to whatever it
 * receives so the UI never renders raw PII for a REVIEWER even if a backend
 * defect were to leak it.
 */

/** Routing outcomes that appear in the investigator queue (Req 22.1). */
export type FlaggedRiskDecision = 'FLAG_FOR_REVIEW' | 'INVESTIGATE';

/** The two Risk_Decisions the investigator queue surfaces. */
export const FLAGGED_DECISIONS: readonly FlaggedRiskDecision[] = [
  'FLAG_FOR_REVIEW',
  'INVESTIGATE',
];

/**
 * A single row in the flagged-claims queue. PII fields (claimantName,
 * claimantSin, address) are optional because they are absent — or masked to
 * `null` — for REVIEWER-role callers (Req 22.6).
 */
export interface FlaggedClaim {
  /** Stable claim identifier (UUID) used for detail navigation. */
  claimId: string;
  /** Aggregated fraud probability in [0, 100]; drives the sort order. */
  riskScore: number;
  /** Routing outcome — always one of the flagged decisions. */
  riskDecision: FlaggedRiskDecision;
  /** ISO-8601 timestamp the claim was received. */
  receivedAt: string;
  /** Employer Business Number associated with the claim (non-PII). */
  employerBn: string;
  /** Claimant full name — PII, masked for REVIEWER. */
  claimantName?: string | null;
  /** Claimant SIN — PII, masked for REVIEWER. */
  claimantSin?: string | null;
  /** Claimant residential address — PII, masked for REVIEWER. */
  address?: string | null;
}

/** Type guard for a flagged Risk_Decision value. */
export function isFlaggedDecision(value: unknown): value is FlaggedRiskDecision {
  return value === 'FLAG_FOR_REVIEW' || value === 'INVESTIGATE';
}

/**
 * Parse an untrusted queue row into a typed FlaggedClaim, or `null` when the
 * payload is malformed or is not one of the flagged decisions. Rejecting rows
 * that are not FLAG_FOR_REVIEW/INVESTIGATE keeps the queue contract intact even
 * if the backend were to return an unexpected decision (Req 22.1).
 */
export function parseFlaggedClaim(payload: unknown): FlaggedClaim | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;

  const claimId = record.claimId ?? record.claim_id;
  const riskScoreRaw = record.riskScore ?? record.risk_score;
  const riskDecision = record.riskDecision ?? record.risk_decision;
  const receivedAt = record.receivedAt ?? record.received_at;
  const employerBn = record.employerBn ?? record.employer_bn;

  if (typeof claimId !== 'string' || claimId.length === 0) {
    return null;
  }
  if (typeof riskScoreRaw !== 'number' || Number.isNaN(riskScoreRaw)) {
    return null;
  }
  if (!isFlaggedDecision(riskDecision)) {
    return null;
  }

  // Clamp the score into the documented [0, 100] range (Req 9.1).
  const riskScore = Math.max(0, Math.min(100, Math.round(riskScoreRaw)));

  const claimantName = record.claimantName ?? record.claimant_name;
  const claimantSin = record.claimantSin ?? record.claimant_sin;
  const address = record.address;

  return {
    claimId,
    riskScore,
    riskDecision,
    receivedAt: typeof receivedAt === 'string' ? receivedAt : '',
    employerBn: typeof employerBn === 'string' ? employerBn : '',
    claimantName: typeof claimantName === 'string' ? claimantName : null,
    claimantSin: typeof claimantSin === 'string' ? claimantSin : null,
    address: typeof address === 'string' ? address : null,
  };
}

/**
 * Sort flagged claims by Risk_Score descending (Req 22.1). Ties are broken by
 * most-recently-received first so the highest-priority, freshest work rises to
 * the top. Returns a new array; the input is not mutated.
 */
export function sortByRiskScoreDescending(
  claims: readonly FlaggedClaim[],
): FlaggedClaim[] {
  return [...claims].sort((a, b) => {
    if (b.riskScore !== a.riskScore) {
      return b.riskScore - a.riskScore;
    }
    return b.receivedAt.localeCompare(a.receivedAt);
  });
}

/**
 * Merge a newly-arrived claim into an existing queue, de-duplicating by
 * claimId (a later event for the same claim replaces the earlier row) and
 * re-sorting by Risk_Score descending. Returns a new array (Req 22.1).
 */
export function mergeFlaggedClaim(
  existing: readonly FlaggedClaim[],
  incoming: FlaggedClaim,
): FlaggedClaim[] {
  const withoutDup = existing.filter((c) => c.claimId !== incoming.claimId);
  return sortByRiskScoreDescending([...withoutDup, incoming]);
}

/**
 * Fetch the flagged-claims queue from the backend, apply role-based PII
 * masking, and return the rows sorted by Risk_Score descending (Req 22.1,
 * 22.6). The HttpOnly session cookie authenticates the request.
 *
 * @param roles roles held by the current user (drives client-side masking).
 * @param signal optional AbortSignal to cancel the request.
 */
export async function fetchFlaggedClaims(
  roles: readonly Role[],
  signal?: AbortSignal,
): Promise<FlaggedClaim[]> {
  const response = await fetch('/portal/investigator/claims', {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'application/json' },
    signal,
  });

  if (!response.ok) {
    throw new Error(
      `Failed to load flagged claims (status ${response.status}).`,
    );
  }

  const payload: unknown = await response.json();
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as { claims?: unknown })?.claims)
      ? (payload as { claims: unknown[] }).claims
      : [];

  const parsed = rows
    .map(parseFlaggedClaim)
    .filter((c): c is FlaggedClaim => c !== null)
    .map((c) => maskFlaggedClaimForRoles(c, roles));

  return sortByRiskScoreDescending(parsed);
}
