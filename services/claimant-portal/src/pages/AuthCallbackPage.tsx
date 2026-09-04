import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from '../auth/AuthContext';
import { completeLogin, StateMismatchError } from '../auth/gckey';

/**
 * Handles the GCKey OAuth 2.0 redirect at `/auth/callback`. Verifies the
 * `state`, forwards the authorization code + PKCE verifier to the backend
 * (which sets the HttpOnly session cookie), refreshes the session, then
 * navigates back to the originally requested route.
 */
export default function AuthCallbackPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [errorKey, setErrorKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function run() {
      try {
        const { returnTo } = await completeLogin(window.location.search);
        await refresh();
        if (!cancelled) {
          navigate(returnTo, { replace: true });
        }
      } catch (err) {
        if (cancelled) {
          return;
        }
        setErrorKey(
          err instanceof StateMismatchError
            ? 'auth.stateMismatch'
            : 'auth.callbackError',
        );
      }
    }

    void run();
    return () => {
      cancelled = true;
    };
  }, [navigate, refresh]);

  if (errorKey) {
    return (
      <section aria-labelledby="auth-error-heading">
        <h1 id="auth-error-heading">{t('errors.generic')}</h1>
        <p role="alert">{t(errorKey)}</p>
      </section>
    );
  }

  return (
    <p role="status" aria-live="polite">
      {t('auth.processingCallback')}
    </p>
  );
}
