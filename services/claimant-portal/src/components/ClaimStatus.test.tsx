import { afterEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';

import i18n from '../i18n/config';
import ClaimStatus from './ClaimStatus';
import type { ClaimStatusView } from '../api/claims';

const CLAIM: ClaimStatusView = {
  claimId: 'CLAIM-123',
  status: 'DECIDED',
  riskDecision: 'AUTO_APPROVE',
  riskScore: 10,
  explanationEn: 'No fraud indicators were found for this claim.',
  explanationFr: "Aucun indicateur de fraude n'a été trouvé pour cette demande.",
  submittedAt: null,
  decidedAt: null,
};

describe('ClaimStatus', () => {
  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('shows status, decision, and the English explanation in EN', async () => {
    await i18n.changeLanguage('en');
    render(<ClaimStatus claim={CLAIM} />);

    expect(screen.getByText('CLAIM-123')).toBeInTheDocument();
    expect(screen.getByText('Decision made')).toBeInTheDocument();
    expect(screen.getByText('Approved')).toBeInTheDocument();
    expect(
      screen.getByText('No fraud indicators were found for this claim.'),
    ).toBeInTheDocument();
  });

  it('shows the French explanation when the language is FR', async () => {
    await i18n.changeLanguage('fr');
    render(<ClaimStatus claim={CLAIM} />);

    expect(
      screen.getByText(
        "Aucun indicateur de fraude n'a été trouvé pour cette demande.",
      ),
    ).toBeInTheDocument();
    // The English copy must not be shown when FR is active.
    expect(
      screen.queryByText('No fraud indicators were found for this claim.'),
    ).not.toBeInTheDocument();
  });

  it('shows a pending message when there is no decision or explanation yet', async () => {
    await i18n.changeLanguage('en');
    render(
      <ClaimStatus
        claim={{
          ...CLAIM,
          riskDecision: null,
          explanationEn: null,
          explanationFr: null,
        }}
      />,
    );
    expect(screen.getByText('Not yet available')).toBeInTheDocument();
    expect(
      screen.getByText(
        'An explanation will be available once your claim has been assessed.',
      ),
    ).toBeInTheDocument();
  });

  it('has no detectable WCAG violations (axe)', async () => {
    await i18n.changeLanguage('en');
    const { container } = render(<ClaimStatus claim={CLAIM} />);
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });
});
