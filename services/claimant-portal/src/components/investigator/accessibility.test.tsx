import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { configureAxe } from 'jest-axe';

import i18n from '../../i18n/config';
import { AuthProvider } from '../../auth/AuthContext';
import SubmitClaimPage from '../../pages/SubmitClaimPage';
import ShapAttributionChart from './ShapAttributionChart';
import FlaggedClaimsList from './FlaggedClaimsList';
import type { Indicator, ShapAttribution } from '../../api/investigatorClaim';

/**
 * Automated accessibility coverage for the investigator dashboard (Task 27.3,
 * Req 1.10, 22.5).
 *
 * These tests drive axe-core (via jest-axe, pinned 9.0.0 / 4.10.2) against the
 * three interactive surfaces called out by the task — the claim submission
 * form, the SHAP attribution chart, and the flagged-claims list — and assert
 * zero violations restricted to the WCAG 2.1 Level A / AA rule sets. They also
 * assert that the SHAP chart's text alternative (a descriptive `role="img"`
 * label, per-bar labels, and a hidden data table with title/axis labels)
 * renders in BOTH official languages (Req 22.5).
 *
 * IMPORTANT CAVEAT: automated axe checks catch only a subset of WCAG 2.1 AA
 * success criteria (roughly 30–40%). Full conformance still requires manual
 * testing with assistive technologies (screen readers, keyboard-only
 * navigation, magnification) and expert accessibility review. A green suite
 * here means "no machine-detectable violations", not "certified AA".
 */

// Constrain axe to the WCAG 2.1 A + AA rule tags so the assertion maps directly
// onto the "WCAG 2.1 Level AA" wording in Req 1.10.
const axe = configureAxe({
  runOnly: {
    type: 'tag',
    values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
  },
});

const indicators: Indicator[] = [
  {
    indicatorId: 'ind-1',
    engine: 'GEOGRAPHIC',
    severity: 'CRITICAL',
    title: 'Implausible commute',
    explanationEn: 'Transport cost exceeds benefit.',
    explanationFr: 'Coût de transport élevé.',
    shapContribution: 30,
  },
  {
    indicatorId: 'ind-2',
    engine: 'DOCUMENT_FORGERY',
    severity: 'MEDIUM',
    title: 'Unusual font',
    explanationEn: 'Font substitution detected.',
    explanationFr: 'Substitution de police détectée.',
    shapContribution: -5,
  },
];

const attributions: ShapAttribution[] = [
  { indicatorId: 'ind-1', featureName: 'geo', contribution: 30 },
  { indicatorId: 'ind-2', featureName: 'font', contribution: -5 },
];

/**
 * Stub `/auth/session` (so AuthProvider resolves to an INVESTIGATOR) and
 * `/portal/investigator/claims` (so the flagged-claims table renders rows),
 * mirroring the pattern used by FlaggedClaimsList.test.tsx.
 */
function mockInvestigatorBackend() {
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

describe('investigator dashboard accessibility (axe, WCAG 2.1 AA)', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
    await i18n.changeLanguage('en');
  });

  it('claim submission form has no WCAG 2.1 AA violations', async () => {
    const { container } = render(<SubmitClaimPage />);
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });

  it('claim submission form has no WCAG 2.1 AA violations in French', async () => {
    await i18n.changeLanguage('fr');
    const { container } = render(<SubmitClaimPage />);
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });

  it('SHAP attribution chart has no WCAG 2.1 AA violations', async () => {
    const { container } = render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });

  it('flagged claims list has no WCAG 2.1 AA violations', async () => {
    mockInvestigatorBackend();
    const { container } = render(
      <MemoryRouter>
        <AuthProvider>
          <FlaggedClaimsList />
        </AuthProvider>
      </MemoryRouter>,
    );

    // Scan only after the queue has loaded so axe sees the real table markup.
    await screen.findByText('92');
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });
});

describe('SHAP chart text alternative renders in both official languages (Req 22.5)', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('provides an EN text alternative: labelled image, per-bar labels, and a data table', () => {
    const { container } = render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );

    // 1. The chart exposes a descriptive English accessible name.
    const img = screen.getByRole('img');
    expect(img.getAttribute('aria-label')).toContain('SHAP feature attribution');
    expect(img.getAttribute('aria-label')).toContain('increase risk');

    // 2. One announceable label per bar, naming the indicator + contribution.
    const barLabels = Array.from(
      container.querySelectorAll('li[aria-label]'),
    ).map((el) => el.getAttribute('aria-label'));
    expect(barLabels).toHaveLength(2);
    expect(
      barLabels.some((l) => l?.includes('Implausible commute')),
    ).toBe(true);

    // 3. The hidden data table mirrors every attribution with EN axis labels.
    const table = screen.getByRole('table');
    expect(table).toHaveTextContent('Implausible commute');
    expect(table).toHaveTextContent('Unusual font');
    expect(table).toHaveTextContent('Increases risk');
    expect(table).toHaveTextContent('Decreases risk');
    expect(
      screen.getByRole('columnheader', { name: /Indicator/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('columnheader', {
        name: /SHAP contribution to risk score/i,
      }),
    ).toBeInTheDocument();
  });

  it('provides a FR text alternative: labelled image, per-bar labels, and a data table', async () => {
    await i18n.changeLanguage('fr');
    const { container } = render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );

    // 1. French descriptive accessible name.
    const img = screen.getByRole('img');
    expect(img.getAttribute('aria-label')).toContain(
      'Attribution des caractéristiques SHAP',
    );
    expect(img.getAttribute('aria-label')).toContain('augmentent le risque');

    // 2. Per-bar labels remain, one per attribution.
    const barLabels = Array.from(
      container.querySelectorAll('li[aria-label]'),
    ).map((el) => el.getAttribute('aria-label'));
    expect(barLabels).toHaveLength(2);
    expect(
      barLabels.some((l) => l?.includes('Implausible commute')),
    ).toBe(true);

    // 3. Hidden data table with FR axis labels and colour-independent direction.
    const table = screen.getByRole('table');
    expect(table).toHaveTextContent('Augmente le risque');
    expect(table).toHaveTextContent('Diminue le risque');
    expect(
      screen.getByRole('columnheader', { name: /Indicateur/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('columnheader', {
        name: /Contribution SHAP à la cote de risque/i,
      }),
    ).toBeInTheDocument();
  });
});
