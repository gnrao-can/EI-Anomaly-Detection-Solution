import { useCallback, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  ApiError,
  requestDataAccess,
  requestDataDeletion,
  type PrivacyRequestResult,
} from '../api/claims';

type Phase = 'idle' | 'confirming-delete' | 'submitting' | 'done' | 'error';

/** Which Privacy Act action produced the current result/announcement. */
type ActionKind = 'access' | 'delete' | null;

/**
 * Privacy Act self-service controls (Req 19.4 data access, Req 19.5 erasure).
 *
 * - The access button fires immediately (non-destructive).
 * - The deletion button opens an inline confirmation first, because erasure is
 *   irreversible; only the explicit confirm actually calls the backend.
 * - Outcomes and errors are announced through an `aria-live` / `role="alert"`
 *   region so screen-reader users are notified (WCAG 2.1 AA, 4.1.3).
 */
export default function PrivacyActions() {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<Phase>('idle');
  const [action, setAction] = useState<ActionKind>(null);
  const [result, setResult] = useState<PrivacyRequestResult | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // After confirming a deletion, move focus to the announcement so it is read.
  const statusRef = useRef<HTMLDivElement>(null);

  const busy = phase === 'submitting';

  const run = useCallback(
    async (
      kind: Exclude<ActionKind, null>,
      call: () => Promise<PrivacyRequestResult>,
    ) => {
      setAction(kind);
      setPhase('submitting');
      setErrorMessage(null);
      setResult(null);
      try {
        const outcome = await call();
        setResult(outcome);
        setPhase('done');
      } catch (err) {
        const message =
          err instanceof ApiError || err instanceof Error
            ? err.message
            : String(err);
        setErrorMessage(message);
        setPhase('error');
      } finally {
        statusRef.current?.focus();
      }
    },
    [],
  );

  const onAccess = useCallback(() => {
    void run('access', requestDataAccess);
  }, [run]);

  const onConfirmDelete = useCallback(() => {
    void run('delete', requestDataDeletion);
  }, [run]);

  const successMessage =
    phase === 'done' && action
      ? t(
          action === 'delete'
            ? 'privacy.deleteSubmitted'
            : 'privacy.accessSubmitted',
          {
            reference: result?.referenceId ?? t('privacy.referencePending'),
          },
        )
      : null;

  return (
    <section aria-labelledby="privacy-heading" className="privacy-actions">
      <h2 id="privacy-heading">{t('privacy.heading')}</h2>
      <p>{t('privacy.intro')}</p>

      <div className="privacy-actions__buttons">
        <button type="button" onClick={onAccess} disabled={busy}>
          {t('privacy.accessButton')}
        </button>

        {phase === 'confirming-delete' ? (
          <div
            role="group"
            aria-label={t('privacy.deleteConfirmHeading')}
            className="privacy-actions__confirm"
          >
            <p>{t('privacy.deleteConfirmMessage')}</p>
            <button
              type="button"
              onClick={onConfirmDelete}
              disabled={busy}
            >
              {t('privacy.deleteConfirmButton')}
            </button>
            <button
              type="button"
              onClick={() => setPhase('idle')}
              disabled={busy}
            >
              {t('privacy.deleteCancelButton')}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setPhase('confirming-delete')}
            disabled={busy}
          >
            {t('privacy.deleteButton')}
          </button>
        )}
      </div>

      <div
        ref={statusRef}
        tabIndex={-1}
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
        className="privacy-actions__status"
      >
        {busy && <span>{t('privacy.submitting')}</span>}
        {successMessage && <span>{successMessage}</span>}
        {phase === 'error' && errorMessage && (
          <span>{t('privacy.error', { message: errorMessage })}</span>
        )}
      </div>
    </section>
  );
}
