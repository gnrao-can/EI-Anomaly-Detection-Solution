import { useTranslation } from 'react-i18next';

import { normaliseLanguage } from '../i18n/config';
import { selectExplanation, type ClaimStatusView } from '../api/claims';

/**
 * Presentational view of a single claim's status, Risk_Decision, and the
 * LLM-generated explanation in the active official language (Req 20.2, 22.1).
 *
 * Pure/presentational: it takes an already-loaded {@link ClaimStatusView} and
 * renders it as an accessible description list. Data loading, error, and
 * privacy actions live in the containing page so this stays easy to test.
 */
export default function ClaimStatus({ claim }: { claim: ClaimStatusView }) {
  const { t, i18n } = useTranslation();
  const language = normaliseLanguage(i18n.language);
  const explanation = selectExplanation(claim, language);

  return (
    <div className="claim-status">
      <dl>
        <div>
          <dt>{t('claimStatus.claimId')}</dt>
          <dd>{claim.claimId}</dd>
        </div>
        <div>
          <dt>{t('claimStatus.statusLabel')}</dt>
          <dd>{t(`claimStatus.status.${claim.status}`)}</dd>
        </div>
        <div>
          <dt>{t('claimStatus.decisionLabel')}</dt>
          <dd>
            {claim.riskDecision
              ? t(`claimStatus.decision.${claim.riskDecision}`)
              : t('claimStatus.decisionPending')}
          </dd>
        </div>
      </dl>

      <section aria-labelledby="claim-explanation-heading" className="panel">
        <h2 id="claim-explanation-heading">{t('claimStatus.explanationLabel')}</h2>
        {explanation ? (
          <p lang={language}>{explanation}</p>
        ) : (
          <p>{t('claimStatus.explanationPending')}</p>
        )}
      </section>
    </div>
  );
}
