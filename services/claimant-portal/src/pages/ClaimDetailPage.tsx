import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import {
  fetchClaimStatus,
  ApiError,
  type ClaimStatusView,
} from '../api/claims';
import ClaimStatus from '../components/ClaimStatus';
import PrivacyActions from '../components/PrivacyActions';

type LoadState =
  | { phase: 'loading' }
  | { phase: 'loaded'; claim: ClaimStatusView }
  | { phase: 'not-found' }
  | { phase: 'error'; message: string };

/**
 * Claimant claim-status page backed by `GET /portal/claims/{claim_id}`.
 * Shows the claim status, Risk_Decision, and the LLM explanation in the active
 * language (Req 20.2, 22.1), plus the Privacy Act self-service controls
 * (Req 19.4, 19.5). Guarded to the CLAIMANT role by the router.
 */
export default function ClaimDetailPage() {
  const { t } = useTranslation();
  const { claimId } = useParams<{ claimId: string }>();
  const [state, setState] = useState<LoadState>({ phase: 'loading' });

  const load = useCallback(async (id: string) => {
    setState({ phase: 'loading' });
    try {
      const claim = await fetchClaimStatus(id);
      setState(
        claim ? { phase: 'loaded', claim } : { phase: 'not-found' },
      );
    } catch (err) {
      const message =
        err instanceof ApiError || err instanceof Error
          ? err.message
          : String(err);
      setState({ phase: 'error', message });
    }
  }, []);

  useEffect(() => {
    if (!claimId) {
      setState({ phase: 'not-found' });
      return;
    }
    void load(claimId);
  }, [claimId, load]);

  return (
    <section aria-labelledby="claim-detail-heading">
      <h1 id="claim-detail-heading">{t('claimStatus.heading')}</h1>

      {state.phase === 'loading' && (
        <p role="status" aria-live="polite">
          {t('app.loading')}
        </p>
      )}

      {state.phase === 'not-found' && (
        <p role="alert">{t('claimStatus.notFound')}</p>
      )}

      {state.phase === 'error' && (
        <div role="alert">
          <p>{t('claimStatus.loadError', { message: state.message })}</p>
          {claimId && (
            <button type="button" onClick={() => void load(claimId)}>
              {t('claimStatus.retry')}
            </button>
          )}
        </div>
      )}

      {state.phase === 'loaded' && (
        <>
          <ClaimStatus claim={state.claim} />
          <PrivacyActions />
        </>
      )}
    </section>
  );
}
