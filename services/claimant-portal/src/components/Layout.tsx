import { Link, Outlet, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from '../auth/AuthContext';
import { ROLES } from '../auth/roles';
import LanguageSwitcher from './LanguageSwitcher';

/**
 * Application shell: skip link, header navigation with RBAC-aware links,
 * language switcher, and the routed content region.
 *
 * Theme is chosen purely by route (visual only — no logic/data change): the
 * investigator dashboard routes (`/dashboard`, `/dashboard/claims/:id`) use the
 * dark "Slate console" theme; every claimant-facing route uses the light
 * navy/amber theme. The choice is expressed as `data-theme` on the shell
 * wrapper so the token palette in theme.css cascades to the whole subtree.
 */
export default function Layout() {
  const { t } = useTranslation();
  const { status, can, login, logout } = useAuth();
  const { pathname } = useLocation();
  const isAuthenticated = status === 'authenticated';
  const isDashboard =
    pathname === '/dashboard' || pathname.startsWith('/dashboard/');

  return (
    <div className="app-shell" data-theme={isDashboard ? 'dark' : 'light'}>
      <a href="#main-content" className="skip-link">
        {t('nav.home')}
      </a>
      <header className="app-header">
        <nav className="app-nav" aria-label={t('app.title')}>
          <ul>
            <li>
              <Link to="/">{t('nav.home')}</Link>
            </li>
            {can([ROLES.CLAIMANT]) && (
              <>
                <li>
                  <Link to="/submit">{t('nav.submitClaim')}</Link>
                </li>
                <li>
                  <Link to="/claims">{t('nav.myClaims')}</Link>
                </li>
              </>
            )}
            {can([ROLES.REVIEWER, ROLES.INVESTIGATOR, ROLES.ADMINISTRATOR]) && (
              <li>
                <Link to="/dashboard">{t('nav.dashboard')}</Link>
              </li>
            )}
          </ul>
          <div className="app-nav-actions">
            <LanguageSwitcher />
            {isAuthenticated ? (
              <button type="button" onClick={() => void logout()}>
                {t('nav.signOut')}
              </button>
            ) : (
              <button type="button" onClick={() => void login()}>
                {t('nav.signIn')}
              </button>
            )}
          </div>
        </nav>
      </header>
      <main id="main-content" className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
