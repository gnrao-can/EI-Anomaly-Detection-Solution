/**
 * PKCE (Proof Key for Code Exchange, RFC 7636) helpers for the GCKey
 * OAuth 2.0 Authorization Code flow.
 *
 * The SPA generates the PKCE `code_verifier` / `code_challenge` and a `state`
 * value, then redirects the browser to the GCKey `/authorize` endpoint. The
 * backend (FastAPI Claimant Portal) performs the code-for-token exchange and
 * sets an HttpOnly, Secure, SameSite=Strict session cookie — the SPA never
 * handles the access or ID tokens directly (design § GCKey OAuth 2.0 Flow).
 */

/** Base64url-encode an ArrayBuffer without padding (RFC 7636 §A). */
function base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Generate a cryptographically random URL-safe string of `length` chars. */
function randomUrlSafe(length: number): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes).slice(0, length);
}

/** Generate a high-entropy PKCE code verifier (43–128 chars per RFC 7636). */
export function generateCodeVerifier(): string {
  // 96 URL-safe characters — comfortably within the 43–128 bound.
  return randomUrlSafe(96);
}

/** Compute the S256 code challenge for a given verifier. */
export async function generateCodeChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return base64UrlEncode(new Uint8Array(digest));
}

/** Generate an opaque anti-CSRF `state` value for the authorization request. */
export function generateState(): string {
  return randomUrlSafe(32);
}

export interface PkceChallenge {
  verifier: string;
  challenge: string;
  method: 'S256';
  state: string;
}

/** Produce a full PKCE challenge bundle for an authorization request. */
export async function createPkceChallenge(): Promise<PkceChallenge> {
  const verifier = generateCodeVerifier();
  const challenge = await generateCodeChallenge(verifier);
  return { verifier, challenge, method: 'S256', state: generateState() };
}
