import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import '../../i18n/config';
import { AuthProvider } from '../../auth/AuthContext';
import InvestigatorClaimDetail from './InvestigatorClaimDetail';

const RAW_DETAIL = {
  claim_id: 'claim-1',
  risk_score: 87,
  risk_decision: 'INVESTIGATE',
  shap_base_value: 10,
  shap_attributions: [
    { indicator_id: 'ind-1', feature_name: 'geo', contribution: 30 },
  ],
  indicators: [
    {
      indicator_id: 'ind-1',
      engine: 'GEOGRAPHIC',
      severity: 'CRITICAL',
      title: 'Implausible commute',
      explanation_en: 'Transport cost exceeds benefit.',
      explanation_fr: 'Coût élevé.',
    },
  ],
  explanation_en: 'Flagged for geographic anomalies.',
  explanation_fr: 'Signalée pour anomalies.',
  claimant_name: 'Jordan Tremblay',
  claimant_sin: '123456789',
  employer_bn: 'BN-123',
};

function mockBackend(role: string, detail: unknown = RAW_DETAIL) {
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
      if (url.includes('/portal/investigator/claims/')) {
        return { ok: true, status: 200, json: async () => detail } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    },
  );
}

function renderDetail(claimId = 'claim-1') {
  return render(
    <AuthProvider>
      <InvestigatorClaimDetail claimId={claimId} />
    </AuthProvider>,
  );
}

describe('InvestigatorClaimDetail', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it('renders risk score, decision, indicators, explanation, and PII for an INVESTIGATOR', async () => {
    mockBackend('INVESTIGATOR');
    renderDetail();

    expect(await screen.findByText('87')).toBeInTheDocument();
    // The indicator title appears both in the indicator list and as the SHAP
    // chart's hidden data-table row label, hence getAllByText.
    expect(screen.getAllByText(/Implausible commute/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Flagged for geographic anomalies/i)).toBeInTheDocument();
    expect(screen.getByText(/Jordan Tremblay/)).toBeInTheDocument();
    // Severity badge label present (colour-independent).
    expect(screen.getByText('Critical')).toBeInTheDocument();
    // Disposition form is rendered.
    expect(
      screen.getByRole('button', { name: /Save disposition/i }),
    ).toBeInTheDocument();
  });

  it('shows a not-found message for a missing claim', async () => {
    mockBackend('INVESTIGATOR', { detail: null });
    // Force a 404 by pointing at a claim the mock treats as unknown.
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/auth/session')) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ subject: 'inv-1', roles: ['INVESTIGATOR'] }),
          } as Response;
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      },
    );
    renderDetail('missing');
    expect(
      await screen.findByText(/This flagged claim could not be found/i),
    ).toBeInTheDocument();
  });
});
