import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import InvestigatorClaimDetail from '../components/investigator/InvestigatorClaimDetail';

/**
 * Investigator per-claim detail route (Req 22.2–22.5). Distinct from the
 * claimant-facing `ClaimDetailPage` (task 26.3). Guarded to the INVESTIGATOR
 * and ADMINISTRATOR roles by the router (Req 22.6).
 */
export default function InvestigatorClaimDetailPage() {
  const { t } = useTranslation();
  const { claimId } = useParams<{ claimId: string }>();

  if (!claimId) {
    return <p role="alert">{t('investigator.detail.notFound')}</p>;
  }

  return <InvestigatorClaimDetail claimId={claimId} />;
}
