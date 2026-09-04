import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { applyProfileLanguage } from '../i18n/config';
import {
  beginLogin,
  fetchSession,
  logout as gckeyLogout,
  type UserSession,
} from './gckey';
import type { Role } from './roles';
import { hasAnyRole } from './roles';

export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated';

export interface AuthContextValue {
  status: AuthStatus;
  session: UserSession | null;
  /** Roles held by the current user (empty when unauthenticated). */
  roles: Role[];
  /** True when the user holds at least one of the given roles. */
  can: (required: readonly Role[]) => boolean;
  /** Begin the GCKey PKCE login, returning to `returnTo` afterwards. */
  login: (returnTo?: string) => Promise<void>;
  /** Sign out and clear the session. */
  logout: () => Promise<void>;
  /** Re-read the session from the backend (e.g. after a callback). */
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }): ReactNode {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [session, setSession] = useState<UserSession | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchSession();
      setSession(next);
      setStatus(next ? 'authenticated' : 'unauthenticated');
      if (next) {
        // Req 20.1 — default UI language to the GCKey profile preference.
        await applyProfileLanguage(next.preferredLanguage);
      }
    } catch {
      setSession(null);
      setStatus('unauthenticated');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (returnTo = '/') => {
    await beginLogin(returnTo);
  }, []);

  const logout = useCallback(async () => {
    await gckeyLogout();
    setSession(null);
    setStatus('unauthenticated');
  }, []);

  const value = useMemo<AuthContextValue>(() => {
    const roles = session?.roles ?? [];
    return {
      status,
      session,
      roles,
      can: (required) => hasAnyRole(roles, required),
      login,
      logout,
      refresh,
    };
  }, [status, session, login, logout, refresh]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider.');
  }
  return ctx;
}
