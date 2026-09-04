import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useAuth } from '../../auth/AuthContext';
import type { SupportedLanguage } from '../../i18n/config';
import {
  fetchInvestigatorClaimDetail,
  selectBilingual,
  InvestigatorApiError,
  type Disposition,
  type InvestigatorClaimDetail as ClaimDetail,
} from '../../api/investigatorClaim';
import IndicatorList from './IndicatorList';
import ShapAttributionChart from './ShapAttributionChart';
import DispositionForm from './DispositionForm';

/**
 * Investigator-facing per-claim detail view (Req 22.2, 22.3, 22.4, 22.5).
 *
 * Named `InvestigatorClaimDetail` to stay distinct from the claimant-facing
 * `ClaimDetailPage` (task 26.3). Loads the full assessment from
 * `GET /portal/investigator/claims/{claim_id}`, then renders:
 *   - Risk_Score and Risk_Decision, plus (unmasked-for-INVESTIGATOR) claimant
 *     identity,
 *   - the LLM explanation in the active official language,
 *   - all Indicators with severity badges (IndicatorList),
 *   - the accessible SHAP attribution chart (ShapAttributionChart),
 *   - the disposition / case-notes form (DispositionForm).
 *
 * PII masking for the REVIEWER role is applied in the API layer
 * (`maskClaimDetailForRoles`), so this component renders whatever it is given.
 * The route is gated to INVESTIGATOR / ADMINISTRATOR by the router.
 */

export interface InvestigatorClaimDetailProps {
  claimId: string;
}

type LoadState =
  | { phase: 'loading' }
  | { phase: 'loaded'; claim: ClaimDetail }
  | { phase: 'not-found' }
  | { phase: 'error'; message: string };

export default function InvestigatorClaimDetail({
  claimId,
}: InvestigatorClaimDetailProps) {
  const { t, i18n } = useTranslation();
  const { roles, status } = useAuth();
  const language = i18n.language as SupportedLanguage;
  const authReady = status === 'authenticated';

  const [state, setState] = useState<LoadState>({ phase: 'loading' });

  const load = useCallback(
    async (id: string, signal?: AbortSignal) => {
      setState({ phase: 'loading' });
      try {
        const claim = await fetchInvestigatorClaimDetail(id, roles, signal);
        if (signal?.aborted) {
          return;
        }
        setState(claim ? { phase: 'loaded', claim } : { phase: 'not-found' });
      } catch (err) {
        if (signal?.aborted) {
          return;
        }
        const message =
          err instanceof InvestigatorApiError || err instanceof Error
            ? err.message
            : String(err);
        setState({ phase: 'error', message });
      }
    },
    [roles],
  );

  useEffect(() => {
    if (!authReady) {
      return;
    }
    const controller = new AbortController();
    void load(claimId, controller.signal);
    return () => controller.abort();
  }, [authReady, claimId, load]);

  const handleDispositionSubmitted = useCallback(
    (disposition: Disposition) => {
      setState((prev) =>
        prev.phase === 'loaded'
          ? { phase: 'loaded', claim: { ...prev.claim, disposition } }
          : prev,
      );
    },
    [],
  );

  return (
    <section aria-labelledby="investigator-detail-heading">
      <h2 id="investigator-detail-heading">
        {t('investigator.detail.title')}
      </h2>

      {state.phase === 'loading' && (
        <p role="status" aria-live="polite">
          {t('app.loading')}
        </p>
      )}

      {state.phase === 'not-found' && (
        <p role="alert">{t('investigator.detail.notFound')}</p>
      )}

      {state.phase === 'error' && (
        <div role="alert">
          <p>
            {t('investigator.detail.loadError', { message: state.message })}
          </p>
          <button type="button" onClick={() => void load(claimId)}>
            {t('investigator.detail.retry')}
          </button>
        </div>
      )}

      {state.phase === 'loaded' && (
        <ClaimDetailBody
          claim={state.claim}
          language={language}
          onDispositionSubmitted={handleDispositionSubmitted}
        />
      )}
    </section>
  );
}

function ClaimDetailBody({
  claim,
  language,
  onDispositionSubmitted,
}: {
  claim: ClaimDetail;
  language: SupportedLanguage;
  onDispositionSubmitted: (disposition: Disposition) => void;
}) {
  const { t } = useTranslation();
  const explanation = selectBilingual(
    claim.explanationEn,
    claim.explanationFr,
    language,
  );

  return (
    <>
      <dl className="claim-summary">
        <div>
          <dt>{t('investigator.detail.claimId')}</dt>
          <dd>{claim.claimId}</dd>
        </div>
        <div>
          <dt>{t('investigator.detail.riskScore')}</dt>
          <dd>{claim.riskScore}</dd>
        </div>
        <div>
          <dt>{t('investigator.detail.riskDecision')}</dt>
          <dd>
            <span
              className={`decision-badge decision-${claim.riskDecision.toLowerCase()}`}
            >
              {t(`investigator.decision.${claim.riskDecision}`)}
            </span>
          </dd>
        </div>
        <div>
          <dt>{t('investigator.detail.employerBn')}</dt>
          <dd>{claim.employerBn}</dd>
        </div>
        {claim.claimantName != null && (
          <div>
            <dt>{t('investigator.detail.claimant')}</dt>
            <dd>
              {claim.claimantName}
              {claim.claimantSin ? ` · ${claim.claimantSin}` : ''}
            </dd>
          </div>
        )}
      </dl>

      <section aria-labelledby="explanation-heading" className="panel">
        <h3 id="explanation-heading" className="section-title">
          {t('investigator.detail.explanation')}
        </h3>
        <p>
          {explanation ?? t('investigator.detail.explanationPending')}
        </p>
      </section>

      <IndicatorList indicators={claim.indicators} />

      <ShapAttributionChart
        attributions={claim.shapAttributions}
        indicators={claim.indicators}
      />

      <DispositionForm
        claimId={claim.claimId}
        initialDisposition={claim.disposition ?? null}
        initialCaseNotes={claim.caseNotes ?? null}
        onSubmitted={onDispositionSubmitted}
      />
    </>
  );
}
