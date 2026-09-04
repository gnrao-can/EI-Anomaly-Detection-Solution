import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import '../../i18n/config';
import { AuthProvider } from '../../auth/AuthContext';
import FlaggedClaimsList from './FlaggedClaimsList';

/**
 * Renders the list inside a real AuthProvider whose session is stubbed via a
 * mocked `/auth/session` response, then a stubbed `/portal/investigator/claims`
 * response. This exercises the true role → masking path (Req 22.6).
 */
function mockBackend(role: string) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/auth/session')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ subject: 'inv-1', roles: [role] }),
        } as Response;
      }
      if (url.includes('/portal/investigator/claims')) {
        return {
          ok: true,
          status: 200,
          json: async () => [
            {
              claim_id: 'high',
              risk_score: 92,
              risk_decision: 'INVESTIGATE',
              employer_bn: 'BN-HIGH',
              claimant_name: 'Jordan Tremblay',
              claimant_sin: '123456789',
              received_at: '2024-03-01T10:00:00Z',
            },
            {
              claim_id: 'mid',
              risk_score: 45,
              risk_decision: 'FLAG_FOR_REVIEW',
              employer_bn: 'BN-MID',
              claimant_name: 'Alex Chen',
              claimant_sin: '987654321',
              received_at: '2024-03-02T10:00:00Z',
            },
          ],
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    },
  );
}

function renderList() {
  // The list renders a "Review case" Link for privileged roles, so a router
  // context is required.
  return render(
    <MemoryRouter>
      <AuthProvider>
        <FlaggedClaimsList />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe('FlaggedClaimsList', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it('shows unmasked claimant PII and score-descending order for an INVESTIGATOR', async () => {
    mockBackend('INVESTIGATOR');
    renderList();

    // Wait for the table to render, then assert order and unmasked PII.
    const rows = await screen.findAllByRole('row');
    // rows[0] is the header row; rows[1] is the first (highest-score) data row.
    expect(rows[1]).toHaveTextContent('92');
    expect(rows[1]).toHaveTextContent('Jordan Tremblay');
    expect(rows[1]).toHaveTextContent('123456789');
    expect(rows[2]).toHaveTextContent('45');
  });

  it('masks claimant PII for a REVIEWER and omits the claimant column', async () => {
    mockBackend('REVIEWER');
    renderList();

    // Risk data is still visible.
    expect(await screen.findByText('92')).toBeInTheDocument();
    // Raw PII must never appear.
    expect(screen.queryByText('Jordan Tremblay')).not.toBeInTheDocument();
    expect(screen.queryByText('123456789')).not.toBeInTheDocument();
    // The claimant column header is not rendered for a REVIEWER.
    expect(
      screen.queryByRole('columnheader', { name: /claimant/i }),
    ).not.toBeInTheDocument();
  });

  it('renders an accessible live-region for the stream status', async () => {
    mockBackend('INVESTIGATOR');
    renderList();
    await waitFor(() =>
      expect(screen.getAllByRole('status').length).toBeGreaterThan(0),
    );
  });
});
