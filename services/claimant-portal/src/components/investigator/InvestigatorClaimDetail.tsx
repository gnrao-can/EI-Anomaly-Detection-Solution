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
import FraudRingGraph from './FraudRingGraph';

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

/**
 * The six Detection_Engines that run concurrently on every claim (Req 2.1).
 * The AI-processing column shows which of them produced Indicators for THIS
 * claim, derived from the `engine` field already on each Indicator — so the
 * display reflects what the pipeline actually did rather than a fixed list.
 */
const ALL_ENGINES = [
  'REPETITIVE_PATTERN',
  'GEOGRAPHIC_ANOMALY',
  'FRAUD_RING',
  'HISTORY_INCONSISTENCY',
  'DOCUMENT_FORGERY',
  'REGIONAL_ANALYSIS',
] as const;

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

  // Which engines produced at least one Indicator for this claim.
  const enginesWithFindings = new Set(
    claim.indicators.map((i) => i.engine).filter(Boolean),
  );

  return (
    <div className="investigator-columns">
      {/* ── Column 1: Claim data ─────────────────────────────── */}
      <div className="investigator-column" aria-labelledby="col-claim-data">
        <h3 id="col-claim-data" className="section-title">
          {t('investigator.columns.claimData')}
        </h3>
        <dl className="claim-summary">
          <div>
            <dt>{t('investigator.detail.claimId')}</dt>
            <dd>{claim.claimId}</dd>
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
        </dl>
      </div>

      {/* ── Column 2: AI processing ──────────────────────────── */}
      <div className="investigator-column" aria-labelledby="col-ai-processing">
        <h3 id="col-ai-processing" className="section-title">
          {t('investigator.columns.aiProcessing')}
        </h3>
        <p className="engines-summary">
          {t('investigator.processing.summary', {
            count: enginesWithFindings.size,
            total: ALL_ENGINES.length,
          })}
        </p>
        <ul className="engine-list">
          {ALL_ENGINES.map((engine) => {
            const ran = enginesWithFindings.has(engine);
            return (
              <li
                key={engine}
                className={`engine-item ${ran ? 'engine-ran' : 'engine-idle'}`}
              >
                <span
                  className={`engine-dot ${ran ? 'engine-dot-active' : ''}`}
                  aria-hidden="true"
                />
                <span className="engine-name">
                  {t(`investigator.engine.${engine}`)}
                </span>
                <span className="engine-status">
                  {ran
                    ? t('investigator.processing.engineRan')
                    : t('investigator.processing.engineNoFinding')}
                </span>
              </li>
            );
          })}
        </ul>

        <section aria-labelledby="explanation-heading" className="panel">
          <h4 id="explanation-heading" className="section-title">
            {t('investigator.detail.explanation')}
          </h4>
          <p>{explanation ?? t('investigator.detail.explanationPending')}</p>
        </section>
      </div>

      {/* ── Column 3: Detection result ───────────────────────── */}
      <div className="investigator-column" aria-labelledby="col-detection-result">
        <h3 id="col-detection-result" className="section-title">
          {t('investigator.columns.detectionResult')}
        </h3>

        <div className="score-panel">
          <div
            className={`score-circle score-${scoreBand(claim.riskScore)}`}
            role="img"
            aria-label={`${t('investigator.detail.riskScore')}: ${claim.riskScore}`}
          >
            <span className="score-num">{claim.riskScore}</span>
            <span className="score-cap">{t('investigator.detail.riskScore')}</span>
          </div>
        </div>

        <IndicatorList indicators={claim.indicators} />

        <ShapAttributionChart
          attributions={claim.shapAttributions}
          indicators={claim.indicators}
        />

        <FraudRingGraph claimId={claim.claimId} />

        <DispositionForm
          claimId={claim.claimId}
          initialDisposition={claim.disposition ?? null}
          initialCaseNotes={claim.caseNotes ?? null}
          onSubmitted={onDispositionSubmitted}
        />
      </div>
    </div>
  );
}

/** Map a 0–100 risk score to a gauge band (matches the demo's colour bands). */
function scoreBand(score: number): 'low' | 'medium' | 'high' | 'critical' {
  if (score <= 29) return 'low';
  if (score <= 69) return 'medium';
  if (score <= 84) return 'high';
  return 'critical';
}
