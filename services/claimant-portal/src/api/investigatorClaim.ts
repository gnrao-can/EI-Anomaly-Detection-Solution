import type { Role } from '../auth/roles';
import type { SupportedLanguage } from '../i18n/config';
import { canViewUnmaskedPii, maskField, maskSin } from './pii';

/**
 * Investigator per-claim detail data access and case-management client
 * (Req 22.2, 22.3, 22.4, 22.5).
 *
 * The backend exposes the full assessment for a flagged claim at
 * `GET /portal/investigator/claims/{claim_id}` — Risk_Score, Risk_Decision,
 * every Indicator with its severity and bilingual explanation, the SHAP
 * attribution vector, and the LLM-generated bilingual summary.
 *
 * Investigators record a disposition via `PUT /api/v1/claims/{claim_id}/disposition`.
 * The Kafka `outcome.feedback` publish happens on the backend when the
 * disposition is CONFIRMED_FRAUD or FALSE_POSITIVE (Req 22.4); the SPA only
 * issues the HTTP request.
 *
 * PII masking (Req 22.6) is enforced authoritatively by the backend based on
 * the caller's role. The client re-applies the same rules to whatever it
 * receives so the UI never renders raw PII for a REVIEWER (defence in depth).
 */

/** Severity levels an Indicator may carry (design § Indicator Model). */
export type Severity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL' | 'UNRESOLVED';

export const SEVERITIES: readonly Severity[] = [
  'CRITICAL',
  'HIGH',
  'MEDIUM',
  'LOW',
  'UNRESOLVED',
];

/** Routing outcomes surfaced in the investigator detail view. */
export type RiskDecision =
  | 'AUTO_APPROVE'
  | 'FLAG_FOR_REVIEW'
  | 'INVESTIGATE'
  | 'INDETERMINATE';

/** Dispositions an investigator may record (Req 22.3). */
export type Disposition =
  | 'CONFIRMED_FRAUD'
  | 'FALSE_POSITIVE'
  | 'NEEDS_FURTHER_REVIEW';

export const DISPOSITIONS: readonly Disposition[] = [
  'CONFIRMED_FRAUD',
  'FALSE_POSITIVE',
  'NEEDS_FURTHER_REVIEW',
];

/** Type guard for a recognised disposition value. */
export function isDisposition(value: unknown): value is Disposition {
  return (
    typeof value === 'string' &&
    (DISPOSITIONS as readonly string[]).includes(value)
  );
}

/**
 * A single Indicator produced by a Detection_Engine (design § Indicator Model).
 * Explanations are delivered as parallel EN/FR text (Req 20.2); the view
 * selects the copy matching the active UI language.
 */
export interface Indicator {
  indicatorId: string;
  engine: string;
  severity: Severity;
  title: string;
  explanationEn: string | null;
  explanationFr: string | null;
  /** Signed SHAP contribution for this Indicator, when available. */
  shapContribution: number | null;
}

/**
 * One SHAP attribution entry (design § ShapAttribution). `contribution` is
 * signed; the sum across all attributions equals risk_score − base_value
 * (SHAP additivity invariant, Req 21.3 / Property 7).
 */
export interface ShapAttribution {
  indicatorId: string;
  featureName: string;
  contribution: number;
}

/**
 * Full investigator-facing assessment for a single flagged claim
 * (Req 22.2). PII fields are optional/masked for REVIEWER-role callers.
 */
export interface InvestigatorClaimDetail {
  claimId: string;
  riskScore: number;
  riskDecision: RiskDecision;
  shapBaseValue: number;
  shapAttributions: ShapAttribution[];
  indicators: Indicator[];
  explanationEn: string | null;
  explanationFr: string | null;
  /** Claimant full name — PII, masked for REVIEWER. */
  claimantName?: string | null;
  /** Claimant SIN — PII, masked for REVIEWER. */
  claimantSin?: string | null;
  /** Employer Business Number (non-PII). */
  employerBn: string;
  /** Disposition already recorded for this claim, if any. */
  disposition?: Disposition | null;
  /** Case notes previously saved with the disposition, if any. */
  caseNotes?: string | null;
}

/** Error thrown when an investigator API call returns a non-OK HTTP status. */
export class InvestigatorApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'InvestigatorApiError';
    this.status = status;
  }
}

function isSeverity(value: unknown): value is Severity {
  return (
    typeof value === 'string' && (SEVERITIES as readonly string[]).includes(value)
  );
}

function isRiskDecision(value: unknown): value is RiskDecision {
  return (
    value === 'AUTO_APPROVE' ||
    value === 'FLAG_FOR_REVIEW' ||
    value === 'INVESTIGATE' ||
    value === 'INDETERMINATE'
  );
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Parse an untrusted Indicator payload into a typed Indicator, or `null` when
 * the payload is malformed. Unknown severities default to UNRESOLVED so the
 * row still renders rather than being silently dropped.
 */
export function parseIndicator(payload: unknown): Indicator | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const indicatorId = record.indicatorId ?? record.indicator_id;
  if (typeof indicatorId !== 'string' || indicatorId.length === 0) {
    return null;
  }
  const severityRaw = record.severity;
  return {
    indicatorId,
    engine: optionalString(record.engine) ?? '',
    severity: isSeverity(severityRaw) ? severityRaw : 'UNRESOLVED',
    title: optionalString(record.title) ?? '',
    explanationEn: optionalString(record.explanationEn ?? record.explanation_en),
    explanationFr: optionalString(record.explanationFr ?? record.explanation_fr),
    shapContribution: optionalNumber(
      record.shapContribution ?? record.shap_contribution,
    ),
  };
}

/**
 * Parse an untrusted SHAP attribution payload, or `null` when malformed. A
 * non-numeric contribution is rejected so the additivity relationship the
 * chart depends on is never corrupted by bad data.
 */
export function parseShapAttribution(payload: unknown): ShapAttribution | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const indicatorId = record.indicatorId ?? record.indicator_id;
  const contribution = optionalNumber(record.contribution);
  if (typeof indicatorId !== 'string' || contribution === null) {
    return null;
  }
  return {
    indicatorId,
    featureName: optionalString(record.featureName ?? record.feature_name) ?? '',
    contribution,
  };
}

/**
 * Parse a raw investigator claim-detail payload defensively. Returns `null`
 * when the payload lacks a claim id, a numeric Risk_Score, or a recognised
 * Risk_Decision, so the view can render an "unavailable" state rather than
 * crash (Req 22.2).
 */
export function parseInvestigatorClaimDetail(
  payload: unknown,
): InvestigatorClaimDetail | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const claimId = record.claimId ?? record.claim_id;
  const riskScoreRaw = record.riskScore ?? record.risk_score;
  const riskDecision = record.riskDecision ?? record.risk_decision;
  if (typeof claimId !== 'string' || claimId.length === 0) {
    return null;
  }
  if (typeof riskScoreRaw !== 'number' || Number.isNaN(riskScoreRaw)) {
    return null;
  }
  if (!isRiskDecision(riskDecision)) {
    return null;
  }

  const riskScore = Math.max(0, Math.min(100, Math.round(riskScoreRaw)));

  const indicatorsRaw = Array.isArray(record.indicators)
    ? record.indicators
    : [];
  const indicators = indicatorsRaw
    .map(parseIndicator)
    .filter((i): i is Indicator => i !== null);

  const attributionsRaw = Array.isArray(
    record.shapAttributions ?? record.shap_attributions,
  )
    ? (record.shapAttributions ?? record.shap_attributions)
    : [];
  const shapAttributions = (attributionsRaw as unknown[])
    .map(parseShapAttribution)
    .filter((a): a is ShapAttribution => a !== null);

  const disposition = record.disposition;

  return {
    claimId,
    riskScore,
    riskDecision,
    shapBaseValue: optionalNumber(
      record.shapBaseValue ?? record.shap_base_value,
    ) ?? 0,
    shapAttributions,
    indicators,
    explanationEn: optionalString(record.explanationEn ?? record.explanation_en),
    explanationFr: optionalString(record.explanationFr ?? record.explanation_fr),
    claimantName: optionalString(record.claimantName ?? record.claimant_name),
    claimantSin: optionalString(record.claimantSin ?? record.claimant_sin),
    employerBn: optionalString(record.employerBn ?? record.employer_bn) ?? '',
    disposition: isDisposition(disposition) ? disposition : null,
    caseNotes: optionalString(record.caseNotes ?? record.case_notes),
  };
}

/**
 * Select the bilingual explanation matching the active UI language (Req 20.2),
 * falling back to the other official language when the preferred one is absent.
 */
export function selectBilingual(
  en: string | null | undefined,
  fr: string | null | undefined,
  language: SupportedLanguage,
): string | null {
  const preferred = language === 'fr' ? fr : en;
  const fallback = language === 'fr' ? en : fr;
  return preferred ?? fallback ?? null;
}

/**
 * Apply role-based PII masking to a claim detail (Req 22.6). Privileged roles
 * (INVESTIGATOR / ADMINISTRATOR) see the record unchanged; all other roles get
 * masked name and SIN while risk data and Indicators remain visible for triage.
 */
export function maskClaimDetailForRoles(
  claim: InvestigatorClaimDetail,
  roles: readonly Role[],
): InvestigatorClaimDetail {
  if (canViewUnmaskedPii(roles)) {
    return claim;
  }
  return {
    ...claim,
    claimantName: maskField(claim.claimantName),
    claimantSin: maskSin(claim.claimantSin),
  };
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
 * Fetch the full assessment for a single flagged claim and apply role-based
 * PII masking (Req 22.2, 22.6). The HttpOnly session cookie authenticates the
 * request. Returns `null` when the claim is not found (404).
 *
 * @param claimId claim identifier (UUID).
 * @param roles roles held by the current user (drives client-side masking).
 * @param signal optional AbortSignal to cancel the request.
 */
export async function fetchInvestigatorClaimDetail(
  claimId: string,
  roles: readonly Role[],
  signal?: AbortSignal,
): Promise<InvestigatorClaimDetail | null> {
  const response = await fetch(
    `/portal/investigator/claims/${encodeURIComponent(claimId)}`,
    {
      method: 'GET',
      credentials: 'include',
      headers: { Accept: 'application/json' },
      signal,
    },
  );

  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new InvestigatorApiError(
      response.status,
      await readErrorMessage(response),
    );
  }

  const payload: unknown = await response.json();
  const parsed = parseInvestigatorClaimDetail(payload);
  return parsed ? maskClaimDetailForRoles(parsed, roles) : null;
}

/** Payload sent when recording a disposition (Req 22.3). */
export interface DispositionSubmission {
  disposition: Disposition;
  caseNotes: string;
}

/**
 * Record an investigator disposition for a claim via
 * `PUT /api/v1/claims/{claim_id}/disposition` (Req 22.3). When the disposition
 * is CONFIRMED_FRAUD or FALSE_POSITIVE the backend publishes the outcome to the
 * Kafka `outcome.feedback` topic as a model feedback signal (Req 22.4); the SPA
 * only issues the HTTP request.
 *
 * @throws InvestigatorApiError when the backend responds with a non-OK status.
 */
export async function submitDisposition(
  claimId: string,
  submission: DispositionSubmission,
): Promise<void> {
  const response = await fetch(
    `/api/v1/claims/${encodeURIComponent(claimId)}/disposition`,
    {
      method: 'PUT',
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        disposition: submission.disposition,
        case_notes: submission.caseNotes,
      }),
    },
  );

  if (!response.ok) {
    throw new InvestigatorApiError(
      response.status,
      await readErrorMessage(response),
    );
  }
}
