/**
 * DEV-ONLY one-click role login helper.
 *
 * In the local compose stack there is no GCKey identity provider, so the real
 * OAuth login flow is a dead end. The portal-backend exposes a dev-only
 * endpoint (`GET /auth/dev/login?role=...`) that mints a real, JWKS-valid
 * GCKey Bearer for the chosen role and sets the normal signed session cookie —
 * so a demonstrator can pick a role and be "logged in" with one click, with no
 * DevTools or manual token/cookie handling.
 *
 * This module only exposes the affordance when the backend advertises dev mode
 * (`GET /auth/dev/enabled`), so it is inert in any real deployment. The SPA
 * never sees the token (it lives in the HttpOnly cookie the browser stores on
 * the redirect); we simply navigate the browser to the endpoint.
 */

import { ROLES, type Role } from './roles';

/** Roles offered by the dev switcher (the two that drive the demo). */
export const DEV_LOGIN_ROLES: readonly Role[] = [
  ROLES.CLAIMANT,
  ROLES.INVESTIGATOR,
];

/**
 * Ask the backend whether the one-click dev login is available. Returns false
 * on any error (treated as "not a dev backend"), so the UI stays hidden in
 * production and when the probe is unreachable.
 */
export async function devLoginEnabled(): Promise<boolean> {
  try {
    const response = await fetch('/auth/dev/enabled', {
      method: 'GET',
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) {
      return false;
    }
    const body: unknown = await response.json();
    return (
      typeof body === 'object' &&
      body !== null &&
      (body as { enabled?: unknown }).enabled === true
    );
  } catch {
    return false;
  }
}

/**
 * Log in as `role` via the dev endpoint. This is a FULL browser navigation (not
 * a fetch) so the endpoint's 302 + `Set-Cookie` are applied by the browser and
 * the app reloads with the new session. `next` is where to land afterwards
 * (defaults to the natural landing for the role).
 */
export function devLoginAs(role: Role, next?: string): void {
  const target =
    next ?? (role === ROLES.CLAIMANT ? '/submit' : '/dashboard');
  const params = new URLSearchParams({ role, next: target });
  window.location.assign(`/auth/dev/login?${params.toString()}`);
}
