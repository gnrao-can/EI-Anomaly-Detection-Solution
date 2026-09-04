import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import i18n from '../i18n/config';
import ClaimDetailPage from './ClaimDetailPage';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
    clone() {
      return this as unknown as Response;
    },
  } as Response;
}

function renderAt(claimId: string) {
  return render(
    <MemoryRouter initialEntries={[`/claims/${claimId}`]}>
      <Routes>
        <Route path="/claims/:claimId" element={<ClaimDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('ClaimDetailPage', () => {
  afterEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage('en');
  });

  it('loads and renders the claim status with privacy controls', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        claim_id: 'CLAIM-9',
        status: 'DECIDED',
        risk_decision: 'FLAG_FOR_REVIEW',
        explanation_en: 'Some indicators require review.',
        explanation_fr: 'Certains indicateurs nécessitent un examen.',
      }),
    );

    renderAt('CLAIM-9');

    expect(await screen.findByText('CLAIM-9')).toBeInTheDocument();
    expect(
      screen.getByText('Some indicators require review.'),
    ).toBeInTheDocument();
    // Privacy Act controls are present on the status page.
    expect(
      screen.getByRole('button', { name: 'Request my data' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Request deletion of my data' }),
    ).toBeInTheDocument();
  });

  it('shows an accessible not-found message for an unknown claim', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(null, 404));

    renderAt('missing');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not find a claim/i);
  });

  it('shows an error with a retry control when loading fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}, 500));

    renderAt('CLAIM-err');

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
    expect(
      screen.getByRole('button', { name: 'Try again' }),
    ).toBeInTheDocument();
  });
});
