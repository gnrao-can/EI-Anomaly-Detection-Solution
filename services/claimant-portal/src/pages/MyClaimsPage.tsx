import { useCallback, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

/**
 * Claimant landing page for tracking claims. Lets the claimant open the status
 * view for a specific Claim_ID, which routes to {@link ClaimDetailPage} at
 * `/claims/:claimId` (the `GET /portal/claims/{claim_id}` view). Guarded to the
 * CLAIMANT role by the router.
 */
export default function MyClaimsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [claimId, setClaimId] = useState('');
  const [error, setError] = useState<string | null>(null);

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const trimmed = claimId.trim();
      if (!trimmed) {
        setError(t('myClaims.claimIdRequired'));
        return;
      }
      setError(null);
      navigate(`/claims/${encodeURIComponent(trimmed)}`);
    },
    [claimId, navigate, t],
  );

  return (
    <section aria-labelledby="my-claims-heading">
      <h1 id="my-claims-heading">{t('nav.myClaims')}</h1>
      <p>{t('myClaims.intro')}</p>

      <form onSubmit={onSubmit} noValidate>
        <label htmlFor="claim-id-input">{t('myClaims.claimIdLabel')}</label>
        <input
          id="claim-id-input"
          name="claimId"
          type="text"
          value={claimId}
          onChange={(event) => setClaimId(event.target.value)}
          aria-describedby={error ? 'claim-id-error' : undefined}
          aria-invalid={error ? true : undefined}
          autoComplete="off"
        />
        {error && (
          <span id="claim-id-error" role="alert">
            {error}
          </span>
        )}
        <button type="submit">{t('myClaims.viewStatus')}</button>
      </form>
    </section>
  );
}
