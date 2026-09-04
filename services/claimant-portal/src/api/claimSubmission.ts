/**
 * Claim submission client for the Claim Intake Service.
 *
 * Submits a claim to `POST /api/v1/claims` as multipart form data (claim
 * fields + optional ROE document upload) per the design contract. The
 * authenticated session is carried by the HttpOnly cookie set during the
 * GCKey PKCE flow, so requests use `credentials: 'include'`.
 *
 * The backend responds with either a success payload containing the assigned
 * `claim_id`, or a structured error payload identifying each invalid field
 * (Req 1.3):
 *
 *   { "errors": [ { "field": "insurable_hours", "code": "MISSING",
 *                   "hint": "Enter the total insurable hours worked." } ] }
 *
 * Requirements: 1.9, 1.10
 */

import type { ClaimFormValues } from '../validation/claim';

/** Endpoint for claim submission (proxied to the Claim Intake Service). */
export const CLAIMS_SUBMIT_ENDPOINT = '/api/v1/claims';

/** A structured, per-field error returned by the Claim Intake Service. */
export interface ServerFieldError {
  field: string;
  code: string;
  /** Optional server-provided correction hint (already localised or generic). */
  hint?: string;
}

/** Successful submission result. */
export interface ClaimSubmitSuccess {
  ok: true;
  claimId: string;
}

/** Failed submission result carrying structured field errors and/or a status. */
export interface ClaimSubmitFailure {
  ok: false;
  /** Structured per-field errors, when the server provided them. */
  fieldErrors: ServerFieldError[];
  /** HTTP status code, when a response was received. */
  status?: number;
  /** True when the request never completed (network/transport failure). */
  network?: boolean;
}

export type ClaimSubmitResult = ClaimSubmitSuccess | ClaimSubmitFailure;

/** Build the multipart body for a claim submission. */
export function buildClaimFormData(
  values: ClaimFormValues,
  roeFile: File | null,
): FormData {
  const body = new FormData();
  body.append('sin', values.sin.trim());
  body.append('employer_bn', values.employerBn.trim());
  body.append('insurable_hours', values.insurableHours.trim());
  body.append('insurable_earnings', values.insurableEarnings.trim());
  body.append('separation_date', values.separationDate.trim());
  body.append('separation_reason', values.separationReason.trim());
  if (roeFile) {
    body.append('roe_document', roeFile, roeFile.name);
  }
  return body;
}

/** Extract a claim id from a variety of plausible success payload shapes. */
function extractClaimId(payload: unknown): string | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const candidate = record.claim_id ?? record.claimId;
  return typeof candidate === 'string' && candidate.length > 0
    ? candidate
    : null;
}

/** Extract structured field errors from an error payload, if present. */
function extractFieldErrors(payload: unknown): ServerFieldError[] {
  if (payload == null || typeof payload !== 'object') {
    return [];
  }
  const record = payload as Record<string, unknown>;
  const rawErrors = record.errors;
  if (!Array.isArray(rawErrors)) {
    return [];
  }
  const result: ServerFieldError[] = [];
  for (const item of rawErrors) {
    if (item == null || typeof item !== 'object') {
      continue;
    }
    const entry = item as Record<string, unknown>;
    if (typeof entry.field === 'string' && typeof entry.code === 'string') {
      result.push({
        field: entry.field,
        code: entry.code,
        hint: typeof entry.hint === 'string' ? entry.hint : undefined,
      });
    }
  }
  return result;
}

/**
 * Submit a claim to the Claim Intake Service.
 *
 * Never throws for an expected server error (4xx/5xx with a JSON body) or a
 * network failure; instead returns a discriminated {@link ClaimSubmitResult}
 * so the UI can render an accessible confirmation or error message.
 */
export async function submitClaim(
  values: ClaimFormValues,
  roeFile: File | null,
): Promise<ClaimSubmitResult> {
  let response: Response;
  try {
    response = await fetch(CLAIMS_SUBMIT_ENDPOINT, {
      method: 'POST',
      credentials: 'include',
      headers: { Accept: 'application/json' },
      body: buildClaimFormData(values, roeFile),
    });
  } catch {
    return { ok: false, fieldErrors: [], network: true };
  }

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (response.ok) {
    const claimId = extractClaimId(payload);
    if (claimId) {
      return { ok: true, claimId };
    }
    // 2xx without a parseable claim id is treated as a failure.
    return { ok: false, fieldErrors: [], status: response.status };
  }

  return {
    ok: false,
    fieldErrors: extractFieldErrors(payload),
    status: response.status,
  };
}
