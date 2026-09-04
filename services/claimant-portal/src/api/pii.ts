import { ROLES, type Role } from '../auth/roles';
import type { FlaggedClaim } from './flaggedClaims';

/**
 * Role-based PII masking for the investigator dashboard (Req 22.6).
 *
 * Per the RBAC role model (design § RBAC Role Model):
 *   - INVESTIGATOR / ADMINISTRATOR — full access, including unmasked PII.
 *   - REVIEWER — may view Risk_Score and Indicators, but claimant PII fields
 *     (name, SIN, address) MUST be masked.
 *
 * The backend enforces masking authoritatively; these helpers apply the same
 * rules client-side so the UI never renders raw PII for a REVIEWER, providing
 * defence in depth (Req 19.1, 22.6).
 */

/** Roles permitted to view unmasked claimant PII (design § RBAC Role Model). */
export const PII_PRIVILEGED_ROLES: readonly Role[] = [
  ROLES.INVESTIGATOR,
  ROLES.ADMINISTRATOR,
];

/** Placeholder rendered in place of a masked PII value. */
export const MASKED_PLACEHOLDER = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';

/**
 * Returns true when the given roles permit viewing unmasked PII. A user with
 * INVESTIGATOR or ADMINISTRATOR sees full data even if they also hold REVIEWER;
 * the most-privileged role wins (Req 22.6).
 */
export function canViewUnmaskedPii(roles: readonly Role[]): boolean {
  return roles.some((role) => PII_PRIVILEGED_ROLES.includes(role));
}

/**
 * Mask a claimant SIN to its last three digits, e.g. `•••••••123`. Non-string
 * or empty inputs mask to the placeholder. Never reveals more than the last
 * three digits regardless of input length.
 */
export function maskSin(sin: string | null | undefined): string {
  if (!sin) {
    return MASKED_PLACEHOLDER;
  }
  const digits = sin.replace(/\D/g, '');
  if (digits.length <= 3) {
    return MASKED_PLACEHOLDER;
  }
  return `${'\u2022'.repeat(digits.length - 3)}${digits.slice(-3)}`;
}

/**
 * Mask a free-form PII string (name, address) entirely. The value itself is
 * discarded — length is not revealed — to avoid leaking any signal (Req 22.6).
 * The parameter is accepted only to document intent at call sites.
 */
export function maskField(_value?: string | null): string {
  return MASKED_PLACEHOLDER;
}

/**
 * Apply role-based PII masking to a single flagged claim (Req 22.6). For
 * privileged roles the claim is returned unchanged. For all other roles
 * (including REVIEWER) the claimant name, SIN, and address are replaced with
 * masked placeholders while non-PII fields (score, decision, employer BN) are
 * preserved so the reviewer can still triage by risk.
 */
export function maskFlaggedClaimForRoles(
  claim: FlaggedClaim,
  roles: readonly Role[],
): FlaggedClaim {
  if (canViewUnmaskedPii(roles)) {
    return claim;
  }
  return {
    ...claim,
    claimantName: maskField(claim.claimantName),
    claimantSin: maskSin(claim.claimantSin),
    address: maskField(claim.address),
  };
}
