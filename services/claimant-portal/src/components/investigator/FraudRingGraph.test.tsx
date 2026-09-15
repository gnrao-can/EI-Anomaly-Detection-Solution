import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import '../../i18n/config';
import FraudRingGraph from './FraudRingGraph';

const AVAILABLE_GRAPH = {
  available: true,
  reason: null,
  employer_bn: '123RT0001',
  cohort_size: 3,
  num_communities: 2,
  cluster_count: 1,
  nodes: [
    { id: '123RT0001', type: 'EMPLOYER', label: 'Employer 123RT0001', community: null, flagged: false },
    { id: 'h1', type: 'CLAIMANT', label: 'Claimant AAA111', community: 0, flagged: true },
    { id: 'h2', type: 'CLAIMANT', label: 'Claimant BBB222', community: 0, flagged: true },
    { id: 'h3', type: 'CLAIMANT', label: 'Claimant CCC333', community: 1, flagged: false },
  ],
  edges: [
    { source: 'h1', target: '123RT0001', kind: 'EMPLOYED_BY' },
    { source: 'h2', target: '123RT0001', kind: 'EMPLOYED_BY' },
    { source: 'h3', target: '123RT0001', kind: 'EMPLOYED_BY' },
    { source: 'h1', target: 'h2', kind: 'SHARED_SURNAME' },
  ],
};

function mockGraph(payload: unknown, status = 200) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: 'x',
      json: async () => payload,
    } as Response;
  });
}

describe('FraudRingGraph', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the network with an accessible image + data table when available', async () => {
    mockGraph(AVAILABLE_GRAPH);
    render(<FraudRingGraph claimId="claim-1" />);

    // The SVG is exposed as an image with a descriptive label.
    const img = await screen.findByRole('img', { name: /Fraud ring network/i });
    expect(img).toBeInTheDocument();

    // Every node appears in the visually-hidden data table (colour-independent).
    expect(screen.getByText('Claimant AAA111')).toBeInTheDocument();
    expect(screen.getByText('Employer 123RT0001')).toBeInTheDocument();
    // The cluster membership is conveyed as text, not colour alone.
    expect(screen.getAllByText(/In cluster/i).length).toBeGreaterThan(0);
  });

  it('shows an honest "unavailable" message when the graph service is down', async () => {
    mockGraph({
      available: false,
      reason: 'GRAPH_SERVICE_UNAVAILABLE',
      nodes: [],
      edges: [],
    });
    render(<FraudRingGraph claimId="claim-1" />);

    expect(
      await screen.findByText(/graph service could not be reached/i),
    ).toBeInTheDocument();
  });

  it('shows a "no network" message when the claim has no employer', async () => {
    mockGraph({ available: false, reason: 'NO_EMPLOYER', nodes: [], edges: [] });
    render(<FraudRingGraph claimId="claim-1" />);

    expect(
      await screen.findByText(/No employer is associated/i),
    ).toBeInTheDocument();
  });
});
