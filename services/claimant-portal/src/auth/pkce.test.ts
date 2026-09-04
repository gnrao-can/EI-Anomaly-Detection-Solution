import { describe, expect, it } from 'vitest';

import {
  createPkceChallenge,
  generateCodeChallenge,
  generateCodeVerifier,
  generateState,
} from './pkce';

const BASE64URL = /^[A-Za-z0-9\-_]+$/;

async function sha256Base64Url(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest('SHA-256', data);
  let binary = '';
  for (const byte of new Uint8Array(digest)) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

describe('pkce', () => {
  it('generates verifiers within the RFC 7636 length bounds (43-128)', () => {
    const verifier = generateCodeVerifier();
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
    expect(verifier).toMatch(BASE64URL);
  });

  it('produces distinct verifiers and states on each call', () => {
    const verifiers = new Set(
      Array.from({ length: 50 }, () => generateCodeVerifier()),
    );
    const states = new Set(Array.from({ length: 50 }, () => generateState()));
    expect(verifiers.size).toBe(50);
    expect(states.size).toBe(50);
  });

  it('computes an S256 challenge that matches an independent SHA-256', async () => {
    // Property: for any verifier, challenge === base64url(sha256(verifier)).
    for (let i = 0; i < 25; i += 1) {
      const verifier = generateCodeVerifier();
      const challenge = await generateCodeChallenge(verifier);
      expect(challenge).toMatch(BASE64URL);
      expect(challenge).toBe(await sha256Base64Url(verifier));
    }
  });

  it('bundles verifier, challenge, method, and state consistently', async () => {
    const bundle = await createPkceChallenge();
    expect(bundle.method).toBe('S256');
    expect(bundle.challenge).toBe(await generateCodeChallenge(bundle.verifier));
    expect(bundle.state).toMatch(BASE64URL);
  });
});
