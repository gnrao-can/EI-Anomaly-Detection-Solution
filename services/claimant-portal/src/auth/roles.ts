/**
 * EI-ADS RBAC role model (design § RBAC Role Model).
 *
 * Roles are derived from GCKey OAuth 2.0 scopes by the backend and returned
 * as part of the authenticated session. The SPA uses them only for UI
 * rendering and client-side route guarding; the backend independently
 * enforces authorization on every API endpoint (Req 19.7, 22.6).
 */
export const ROLES = {
  CLAIMANT: 'CLAIMANT',
  REVIEWER: 'REVIEWER',
  INVESTIGATOR: 'INVESTIGATOR',
  ADMINISTRATOR: 'ADMINISTRATOR',
} as const;

export type Role = (typeof ROLES)[keyof typeof ROLES];

export const ALL_ROLES: readonly Role[] = Object.values(ROLES);

/** Type guard for a well-formed role string. */
export function isRole(value: unknown): value is Role {
  return typeof value === 'string' && (ALL_ROLES as string[]).includes(value);
}

/**
 * Returns true when the user holds at least one of the required roles.
 * An empty `required` list means the route only requires authentication.
 */
export function hasAnyRole(
  userRoles: readonly Role[],
  required: readonly Role[],
): boolean {
  if (required.length === 0) {
    return true;
  }
  return required.some((role) => userRoles.includes(role));
}
