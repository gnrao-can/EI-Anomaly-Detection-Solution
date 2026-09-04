import { createPkceChallenge } from './pkce';
import type { Role } from './roles';
import { isRole } from './roles';

/**
 * Client-side driver for the GCKey OAuth 2.0 Authorization Code + PKCE flow.
 *
 * Flow (design § GCKey OAuth 2.0 Flow):
 *   1. `beginLogin` — SPA creates PKCE verifier/challenge + state, stashes the
 *      verifier and state in sessionStorage, and redirects the browser to the
 *      GCKey `/authorize` endpoint.
 *   2. GCKey authenticates the user (with MFA) and redirects back to
 *      `/auth/callback?code=…&state=…`.
 *   3. `completeLogin` — SPA verifies `state`, then POSTs the `code` +
 *      `code_verifier` to the backend, which exchanges them with GCKey and
 *      sets an HttpOnly session cookie. The SPA never sees the tokens.
 *   4. `fetchSession` — SPA reads the authenticated identity (roles + GCKey
 *      profile language) from the backend using the session cookie.
 */

const PKCE_VERIFIER_KEY = 'ei-ads.pkce_verifier';
const PKCE_STATE_KEY = 'ei-ads.pkce_state';
const RETURN_TO_KEY = 'ei-ads.return_to';

/** OAuth config sourced from build-time env (see vite-env.d.ts). */
const config = {
  authorizeUrl: import.meta.env.VITE_GCKEY_AUTHORIZE_URL ?? '/auth/authorize',
  clientId: import.meta.env.VITE_GCKEY_CLIENT_ID ?? 'ei-ads-local',
  redirectUri:
    import.meta.env.VITE_GCKEY_REDIRECT_URI ??
    `${globalThis.location?.origin ?? 'http://localhost:3000'}/auth/callback`,
  scopes: import.meta.env.VITE_GCKEY_SCOPES ?? 'openid profile ei-ads',
};

export interface UserSession {
  /** Authenticated subject identifier (GCKey `sub`). */
  subject: string;
  /** EI-ADS roles derived from GCKey scopes by the backend. */
  roles: Role[];
  /** Preferred official language from the GCKey profile ("en" | "fr"). */
  preferredLanguage: string | null;
  /** Optional display name for the signed-in user. */
  displayName?: string;
}

export class StateMismatchError extends Error {
  constructor() {
    super('OAuth state parameter did not match the stored value.');
    this.name = 'StateMismatchError';
  }
}

/**
 * Step 1 — begin the PKCE login by redirecting the browser to GCKey.
 * @param returnTo path to return to after a successful sign-in.
 */
export async function beginLogin(returnTo = '/'): Promise<void> {
  const pkce = await createPkceChallenge();

  const storage = window.sessionStorage;
  storage.setItem(PKCE_VERIFIER_KEY, pkce.verifier);
  storage.setItem(PKCE_STATE_KEY, pkce.state);
  storage.setItem(RETURN_TO_KEY, returnTo);

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    scope: config.scopes,
    state: pkce.state,
    code_challenge: pkce.challenge,
    code_challenge_method: pkce.method,
  });

  window.location.assign(`${config.authorizeUrl}?${params.toString()}`);
}

export interface CallbackResult {
  /** Path the user intended to reach before authentication. */
  returnTo: string;
}

/**
 * Step 3 — complete the login. Verifies `state`, forwards the authorization
 * `code` + `code_verifier` to the backend which performs the token exchange
 * and sets the HttpOnly session cookie.
 *
 * @param search the callback query string (e.g. `window.location.search`).
 */
export async function completeLogin(search: string): Promise<CallbackResult> {
  const params = new URLSearchParams(search);
  const code = params.get('code');
  const returnedState = params.get('state');
  const error = params.get('error');

  const storage = window.sessionStorage;
  const expectedState = storage.getItem(PKCE_STATE_KEY);
  const verifier = storage.getItem(PKCE_VERIFIER_KEY);
  const returnTo = storage.getItem(RETURN_TO_KEY) ?? '/';

  // Clear one-time PKCE material regardless of outcome.
  storage.removeItem(PKCE_STATE_KEY);
  storage.removeItem(PKCE_VERIFIER_KEY);
  storage.removeItem(RETURN_TO_KEY);

  if (error) {
    throw new Error(`GCKey authorization failed: ${error}`);
  }
  if (!code || !returnedState || !verifier) {
    throw new Error('Incomplete GCKey callback parameters.');
  }
  if (!expectedState || returnedState !== expectedState) {
    throw new StateMismatchError();
  }

  const response = await fetch('/auth/callback', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code,
      code_verifier: verifier,
      redirect_uri: config.redirectUri,
    }),
  });

  if (!response.ok) {
    throw new Error(`Token exchange failed with status ${response.status}.`);
  }

  return { returnTo };
}

/** Parse a raw session payload from the backend into a typed UserSession. */
export function parseSession(payload: unknown): UserSession | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const subject = record.subject;
  if (typeof subject !== 'string' || subject.length === 0) {
    return null;
  }
  const rawRoles = Array.isArray(record.roles) ? record.roles : [];
  const roles = rawRoles.filter(isRole);
  const preferredLanguage =
    typeof record.preferredLanguage === 'string'
      ? record.preferredLanguage
      : null;
  const displayName =
    typeof record.displayName === 'string' ? record.displayName : undefined;

  return { subject, roles, preferredLanguage, displayName };
}

/**
 * Step 4 — read the authenticated session from the backend using the
 * HttpOnly cookie. Returns null when unauthenticated (HTTP 401).
 */
export async function fetchSession(): Promise<UserSession | null> {
  const response = await fetch('/auth/session', {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });

  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Failed to load session (status ${response.status}).`);
  }

  const payload: unknown = await response.json();
  return parseSession(payload);
}

/** Sign out by asking the backend to clear the HttpOnly session cookie. */
export async function logout(): Promise<void> {
  await fetch('/auth/logout', {
    method: 'POST',
    credentials: 'include',
  });
}
