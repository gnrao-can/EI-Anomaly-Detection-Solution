import { useEffect, useState } from 'react';

import { useAuth } from '../auth/AuthContext';
import { DEV_LOGIN_ROLES, devLoginAs, devLoginEnabled } from '../auth/devLogin';
import type { Role } from '../auth/roles';

/**
 * DEV-ONLY one-click role switcher (no GCKey IdP locally).
 *
 * Renders a small "Demo login" control in the header ONLY when the backend
 * reports dev mode (`GET /auth/dev/enabled`) — so it never appears in a real
 * deployment. Picking a role navigates the browser to the dev-login endpoint,
 * which sets the session cookie and redirects to the role's landing page. This
 * replaces all the manual `mint-session-cookie` / DevTools cookie handling: a
 * demonstrator switches between CLAIMANT (submit a claim) and INVESTIGATOR
 * (review the dashboard) with a single click.
 */
export default function DevRoleSwitcher() {
  const { roles, status } = useAuth();
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void devLoginEnabled().then((ok) => {
      if (!cancelled) {
        setEnabled(ok);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!enabled) {
    return null;
  }

  const current: Role | null =
    status === 'authenticated' && roles.length > 0 ? roles[0] : null;

  return (
    <div
      className="dev-role-switcher"
      role="group"
      aria-label="Demo login (development only)"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '0.4rem',
        padding: '0.15rem 0.5rem',
        border: '1px dashed currentColor',
        borderRadius: '6px',
        fontSize: '0.8rem',
        opacity: 0.9,
      }}
    >
      <span aria-hidden="true" title="Local development only">
        🔧 Demo as:
      </span>
      {DEV_LOGIN_ROLES.map((role) => {
        const isActive = current === role;
        return (
          <button
            key={role}
            type="button"
            onClick={() => devLoginAs(role)}
            aria-pressed={isActive}
            title={`Sign in as ${role} (development only)`}
            style={{
              fontWeight: isActive ? 700 : 400,
              textDecoration: isActive ? 'underline' : 'none',
            }}
          >
            {role.charAt(0) + role.slice(1).toLowerCase()}
          </button>
        );
      })}
    </div>
  );
}
