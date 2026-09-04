import { useTranslation } from 'react-i18next';

import FlaggedClaimsList from '../components/investigator/FlaggedClaimsList';

/**
 * Investigator dashboard. Surfaces the flagged-claims queue with real-time
 * updates (Req 22.1) and enforces PII masking for the REVIEWER role via the
 * queue component (Req 22.6). The route is guarded to the REVIEWER,
 * INVESTIGATOR, and ADMINISTRATOR roles by RequireAuth (design § RBAC).
 *
 * The per-claim detail view, SHAP chart, and case-management UI are added in
 * task 27.2.
 */
export default function DashboardPage() {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="dashboard-heading">
      <h1 id="dashboard-heading">{t('nav.dashboard')}</h1>
      <FlaggedClaimsList />
    </section>
  );
}
