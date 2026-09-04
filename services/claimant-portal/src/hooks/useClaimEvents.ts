import { useEffect, useRef, useState } from 'react';

import type { Role } from '../auth/roles';
import { maskFlaggedClaimForRoles } from '../api/pii';
import { parseFlaggedClaim, type FlaggedClaim } from '../api/flaggedClaims';

/**
 * Real-time flagged-claims stream via Server-Sent Events (Req 22.1).
 *
 * The backend publishes new/updated flagged claims on
 * `GET /portal/events/claims` as an SSE stream. This hook opens an
 * `EventSource`, parses each message into a FlaggedClaim, applies role-based
 * PII masking (Req 22.6), and hands the result to `onClaim`. The HttpOnly
 * session cookie authenticates the same-origin connection.
 */

/** Connection state of the SSE stream, surfaced for accessible status text. */
export type StreamStatus = 'connecting' | 'open' | 'closed';

export interface UseClaimEventsOptions {
  /** Roles held by the current user (drives PII masking on each event). */
  roles: readonly Role[];
  /** Invoked for every valid flagged claim received on the stream. */
  onClaim: (claim: FlaggedClaim) => void;
  /**
   * When false the stream is not opened (e.g. before auth resolves). Defaults
   * to true.
   */
  enabled?: boolean;
  /** SSE endpoint; overridable for tests. Defaults to the design's path. */
  url?: string;
}

/** SSE endpoint for real-time flagged-claim updates (Req 22.1). */
export const CLAIM_EVENTS_URL = '/portal/events/claims';

/**
 * Subscribe to the flagged-claims SSE stream. Returns the current connection
 * status so the UI can render an accessible live-region indicator.
 *
 * The `onClaim` callback is stored in a ref so that a changing callback
 * identity does not tear down and re-open the connection on every render.
 */
export function useClaimEvents({
  roles,
  onClaim,
  enabled = true,
  url = CLAIM_EVENTS_URL,
}: UseClaimEventsOptions): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>('connecting');

  const onClaimRef = useRef(onClaim);
  onClaimRef.current = onClaim;

  const rolesRef = useRef(roles);
  rolesRef.current = roles;

  useEffect(() => {
    if (!enabled) {
      setStatus('closed');
      return;
    }
    if (typeof EventSource === 'undefined') {
      // Environment without SSE support (e.g. some test runners). The initial
      // fetch still populates the list; we simply do not stream updates.
      setStatus('closed');
      return;
    }

    setStatus('connecting');
    const source = new EventSource(url, { withCredentials: true });

    source.onopen = () => setStatus('open');

    source.onmessage = (event: MessageEvent<string>) => {
      let payload: unknown;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return; // Ignore malformed frames rather than crashing the stream.
      }
      const claim = parseFlaggedClaim(payload);
      if (claim) {
        onClaimRef.current(maskFlaggedClaimForRoles(claim, rolesRef.current));
      }
    };

    source.onerror = () => {
      // EventSource auto-reconnects; reflect the transient drop in the UI.
      setStatus(source.readyState === EventSource.CLOSED ? 'closed' : 'connecting');
    };

    return () => {
      source.close();
      setStatus('closed');
    };
  }, [enabled, url]);

  return status;
}
