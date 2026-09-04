import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import '../i18n/config';
import { AuthProvider } from './AuthContext';
import { RequireAuth } from './RequireAuth';
import { ROLES } from './roles';

function mockSessionResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function renderGuard(roles: readonly (typeof ROLES)[keyof typeof ROLES][]) {
  return render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <AuthProvider>
        <RequireAuth roles={roles}>
          <p>Protected content</p>
        </RequireAuth>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe('RequireAuth', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it('renders guarded content when the user holds a required role', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      mockSessionResponse({ subject: 'sub-1', roles: ['INVESTIGATOR'] }),
    );

    renderGuard([ROLES.INVESTIGATOR, ROLES.ADMINISTRATOR]);

    expect(await screen.findByText('Protected content')).toBeInTheDocument();
  });

  it('shows an accessible access-denied message when the role is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      mockSessionResponse({ subject: 'sub-2', roles: ['CLAIMANT'] }),
    );

    renderGuard([ROLES.INVESTIGATOR]);

    const alert = await screen.findByRole('alert');
    expect(alert).toBeInTheDocument();
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument();
  });

  it('initiates the GCKey login redirect for unauthenticated users', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      mockSessionResponse(null, 401),
    );

    // jsdom's window.location.assign is non-configurable, so replace the whole
    // location object with a minimal mock that records the redirect target.
    const assign = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...originalLocation, assign, origin: originalLocation.origin },
    });

    try {
      renderGuard([ROLES.INVESTIGATOR]);

      await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
      const target = assign.mock.calls[0][0] as string;
      expect(target).toContain('response_type=code');
      expect(target).toContain('code_challenge_method=S256');
      expect(target).toContain('code_challenge=');
      expect(target).toContain('state=');
    } finally {
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: originalLocation,
      });
    }
  });
});
