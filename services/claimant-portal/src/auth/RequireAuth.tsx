import { useEffect, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from './AuthContext';
import type { Role } from './roles';

export interface RequireAuthProps {
  /**
   * Roles allowed to view the guarded route. An empty list means any
   * authenticated user is permitted.
   */
  roles?: readonly Role[];
  children: ReactNode;
}

/**
 * Client-side route guard enforcing authentication and RBAC (Req 22.6).
 *
 * Unauthenticated users are redirected into the GCKey PKCE login, preserving
 * the intended destination. Authenticated users lacking a required role are
 * shown an accessible "access denied" message rather than being redirected,
 * so screen-reader users are informed of the reason.
 *
 * Note: this is a UX affordance only. The backend independently enforces
 * authorization on every API call (design § RBAC Role Model).
 */
export function RequireAuth({ roles = [], children }: RequireAuthProps): ReactNode {
  const { status, can, login } = useAuth();
  const location = useLocation();
  const { t } = useTranslation();

  const isAuthenticated = status === 'authenticated';

  useEffect(() => {
    if (status === 'unauthenticated') {
      const returnTo = `${location.pathname}${location.search}`;
      void login(returnTo);
    }
  }, [status, location.pathname, location.search, login]);

  if (status === 'loading') {
    return (
      <p role="status" aria-live="polite">
        {t('app.loading')}
      </p>
    );
  }

  if (!isAuthenticated) {
    return (
      <p role="status" aria-live="polite">
        {t('auth.signingIn')}
      </p>
    );
  }

  if (!can(roles)) {
    return (
      <section aria-labelledby="forbidden-heading">
        <h1 id="forbidden-heading">{t('authz.forbiddenTitle')}</h1>
        <p role="alert">{t('authz.forbiddenMessage')}</p>
      </section>
    );
  }

  return <>{children}</>;
}
