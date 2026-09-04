import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from '../../auth/AuthContext';
import { canViewUnmaskedPii } from '../../api/pii';
import {
  fetchFlaggedClaims,
  mergeFlaggedClaim,
  type FlaggedClaim,
} from '../../api/flaggedClaims';
import { useClaimEvents } from '../../hooks/useClaimEvents';

/**
 * Flagged-claims queue for the investigator dashboard (Req 22.1, 22.6).
 *
 * Loads the current queue from `GET /portal/investigator/claims`, then keeps it
 * fresh in near-real-time by merging Server-Sent Events from
 * `GET /portal/events/claims`. Rows are sorted by Risk_Score descending.
 *
 * PII columns (name, SIN, address) are masked for REVIEWER-role users; the
 * table omits the PII column header entirely for those users and renders masked
 * placeholders, matching the backend's authoritative masking (Req 22.6). The
 * route is already gated to REVIEWER/INVESTIGATOR/ADMINISTRATOR by RequireAuth.
 */

type LoadState = 'loading' | 'ready' | 'error';

function formatReceivedAt(iso: string, locale: string): string {
  if (!iso) {
    return '';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export default function FlaggedClaimsList() {
  const { t, i18n } = useTranslation();
  const { roles, status } = useAuth();
  const showPii = canViewUnmaskedPii(roles);
  const authReady = status === 'authenticated';

  const [claims, setClaims] = useState<FlaggedClaim[]>([]);
  const [state, setState] = useState<LoadState>('loading');

  // Initial load of the queue. Deferred until authentication resolves so the
  // request — and the client-side PII masking — use the real role set (Req 22.6).
  useEffect(() => {
    if (!authReady) {
      return;
    }
    const controller = new AbortController();
    setState('loading');
    fetchFlaggedClaims(roles, controller.signal)
      .then((rows) => {
        setClaims(rows);
        setState('ready');
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setState('error');
        // Surface for diagnostics; the UI shows a friendly message.
        console.error('Failed to load flagged claims', error);
      });
    return () => controller.abort();
  }, [roles, authReady]);

  // Real-time updates: merge each streamed claim into the sorted queue.
  const handleIncoming = useCallback((incoming: FlaggedClaim) => {
    setClaims((prev) => mergeFlaggedClaim(prev, incoming));
  }, []);

  const streamStatus = useClaimEvents({
    roles,
    onClaim: handleIncoming,
    enabled: authReady && state !== 'error',
  });

  const decisionLabel = (decision: FlaggedClaim['riskDecision']): string =>
    decision === 'INVESTIGATE'
      ? t('dashboard.decision.investigate')
      : t('dashboard.decision.flagForReview');

  return (
    <section aria-labelledby="flagged-claims-heading" className="panel">
      <h2 id="flagged-claims-heading" className="section-title">
        {t('dashboard.flaggedClaims.title')}
      </h2>

      {/* Accessible live-region reflecting the real-time connection state. */}
      <p role="status" aria-live="polite" className="stream-status">
        {streamStatus === 'open'
          ? t('dashboard.stream.live')
          : streamStatus === 'connecting'
            ? t('dashboard.stream.connecting')
            : t('dashboard.stream.offline')}
      </p>

      {state === 'loading' && (
        <p role="status" aria-live="polite">
          {t('app.loading')}
        </p>
      )}

      {state === 'error' && (
        <p role="alert">{t('dashboard.flaggedClaims.loadError')}</p>
      )}

      {state === 'ready' && claims.length === 0 && (
        <p>{t('dashboard.flaggedClaims.empty')}</p>
      )}

      {state === 'ready' && claims.length > 0 && (
        <table>
          <caption className="sr-only">
            {t('dashboard.flaggedClaims.caption')}
          </caption>
          <thead>
            <tr>
              <th scope="col">{t('dashboard.column.riskScore')}</th>
              <th scope="col">{t('dashboard.column.decision')}</th>
              {showPii && (
                <th scope="col">{t('dashboard.column.claimant')}</th>
              )}
              <th scope="col">{t('dashboard.column.employerBn')}</th>
              <th scope="col">{t('dashboard.column.receivedAt')}</th>
              {showPii && (
                <th scope="col">{t('dashboard.column.actions')}</th>
              )}
            </tr>
          </thead>
          <tbody>
            {claims.map((claim) => (
              <tr key={claim.claimId}>
                <td>{claim.riskScore}</td>
                <td>
                  <span
                    className={`decision-badge decision-${claim.riskDecision.toLowerCase()}`}
                  >
                    {decisionLabel(claim.riskDecision)}
                  </span>
                </td>
                {showPii && (
                  <td>
                    {claim.claimantName ?? ''}
                    {claim.claimantSin ? ` · ${claim.claimantSin}` : ''}
                  </td>
                )}
                <td>{claim.employerBn}</td>
                <td>
                  {formatReceivedAt(claim.receivedAt, i18n.language)}
                </td>
                {showPii && (
                  <td>
                    <Link to={`/dashboard/claims/${claim.claimId}`}>
                      {t('dashboard.action.review')}
                    </Link>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
