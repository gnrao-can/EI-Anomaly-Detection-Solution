import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import i18n from '../../i18n/config';
import ShapAttributionChart from './ShapAttributionChart';
import type { Indicator, ShapAttribution } from '../../api/investigatorClaim';

const indicators: Indicator[] = [
  {
    indicatorId: 'ind-1',
    engine: 'GEOGRAPHIC',
    severity: 'CRITICAL',
    title: 'Implausible commute',
    explanationEn: 'x',
    explanationFr: 'y',
    shapContribution: 30,
  },
  {
    indicatorId: 'ind-2',
    engine: 'DOCUMENT_FORGERY',
    severity: 'MEDIUM',
    title: 'Unusual font',
    explanationEn: 'x',
    explanationFr: 'y',
    shapContribution: -5,
  },
];

const attributions: ShapAttribution[] = [
  { indicatorId: 'ind-1', featureName: 'geo', contribution: 30 },
  { indicatorId: 'ind-2', featureName: 'font', contribution: -5 },
];

describe('ShapAttributionChart', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });
  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('exposes the chart as a labelled image (Req 22.5)', () => {
    render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );
    const img = screen.getByRole('img');
    expect(img.getAttribute('aria-label')).toContain('SHAP feature attribution');
  });

  it('emits a per-bar aria-label for each attribution (Req 22.5)', () => {
    const { container } = render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );
    const barLabels = Array.from(
      container.querySelectorAll('li[aria-label]'),
    ).map((el) => el.getAttribute('aria-label'));
    expect(barLabels).toHaveLength(2);
    expect(
      barLabels.some(
        (l) => l?.includes('Implausible commute') && l.includes('contribution'),
      ),
    ).toBe(true);
    expect(
      barLabels.some((l) => l?.includes('Unusual font')),
    ).toBe(true);
  });

  it('renders a screen-reader data table mirroring every attribution', () => {
    render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );
    const table = screen.getByRole('table');
    const rows = within(table).getAllByRole('row');
    // header + 2 data rows
    expect(rows).toHaveLength(3);
    expect(table).toHaveTextContent('Implausible commute');
    expect(table).toHaveTextContent('Unusual font');
    // Direction text conveys sign without relying on colour (WCAG 1.4.1).
    expect(table).toHaveTextContent('Increases risk');
    expect(table).toHaveTextContent('Decreases risk');
  });

  it('labels the table columns with EN axis titles', () => {
    render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );
    expect(
      screen.getByRole('columnheader', { name: /Indicator/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('columnheader', {
        name: /SHAP contribution to risk score/i,
      }),
    ).toBeInTheDocument();
  });

  it('renders French axis labels when the language is FR', async () => {
    await i18n.changeLanguage('fr');
    render(
      <ShapAttributionChart attributions={attributions} indicators={indicators} />,
    );
    expect(
      screen.getByRole('columnheader', { name: /Indicateur/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain(
      'Attribution des caractéristiques SHAP',
    );
  });

  it('renders an empty state with no attributions', () => {
    render(<ShapAttributionChart attributions={[]} indicators={[]} />);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(
      screen.getByText(/No SHAP attributions are available/i),
    ).toBeInTheDocument();
  });
});
