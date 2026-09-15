import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  fetchClaimGraph,
  type FraudRingGraph as FraudRingGraphData,
  type GraphEdge,
  type GraphNode,
} from '../../api/investigatorGraph';
import { InvestigatorApiError } from '../../api/investigatorClaim';

/**
 * Fraud-ring network graph for the investigator detail view (Req 22.2 surface).
 *
 * Renders the employer hub at the centre with the claimant cohort arranged on a
 * ring around it (a deterministic, force-free layout — no animation, stable
 * across renders). Claimants that belong to a connected community cluster
 * (Req 5.5) are coloured red and joined by shared-surname / shared-address
 * edges; unclustered claimants are grey. This mirrors the "Fraud Ring" network
 * in the executive demo, driven by REAL Graph Service data.
 *
 * Honest degradation (grounding rule 1): the data comes from a live Graph
 * Service read with no persisted fallback, so when the graph is unavailable
 * (no employer BN, service down, or empty cohort) the component shows a plain
 * "no network data" message with the reason — never a fabricated ring.
 *
 * Accessibility (WCAG 2.1 AA):
 *   - The SVG is `role="img"` with a bilingual descriptive label summarising
 *     the cohort size and cluster count.
 *   - Node colour is never the only cue: clustered nodes are also listed in a
 *     visually-hidden data table that names each node, its type, and whether it
 *     is part of a cluster — the authoritative screen-reader alternative.
 */

export interface FraudRingGraphProps {
  claimId: string;
}

type LoadState =
  | { phase: 'loading' }
  | { phase: 'loaded'; graph: FraudRingGraphData }
  | { phase: 'error'; message: string };

// Deterministic palette for community clusters. Index by community id modulo
// the palette length. The employer hub and unclustered claimants use fixed
// colours below.
const CLUSTER_FILLS = ['#dc2626', '#7c3aed', '#ea580c', '#0891b2', '#b45309'];
const HUB_FILL = '#1e3a5f';
const UNCLUSTERED_FILL = '#64748b';

const VIEW_W = 320;
const VIEW_H = 260;
const CX = VIEW_W / 2;
const CY = VIEW_H / 2;
const RING_R = 96;

interface PositionedNode extends GraphNode {
  x: number;
  y: number;
}

function layoutNodes(nodes: readonly GraphNode[]): {
  hub: PositionedNode | null;
  claimants: PositionedNode[];
} {
  const hubNode = nodes.find((n) => n.type === 'EMPLOYER') ?? null;
  const claimantNodes = nodes.filter((n) => n.type === 'CLAIMANT');
  const hub: PositionedNode | null = hubNode
    ? { ...hubNode, x: CX, y: CY }
    : null;

  // Evenly distribute claimants on a ring; deterministic by array order.
  const n = claimantNodes.length;
  const claimants: PositionedNode[] = claimantNodes.map((node, i) => {
    const angle = n === 0 ? 0 : (2 * Math.PI * i) / n - Math.PI / 2;
    return {
      ...node,
      x: CX + RING_R * Math.cos(angle),
      y: CY + RING_R * Math.sin(angle),
    };
  });
  return { hub, claimants };
}

function nodeFill(node: GraphNode): string {
  if (node.type === 'EMPLOYER') {
    return HUB_FILL;
  }
  if (node.flagged && node.community != null) {
    return CLUSTER_FILLS[node.community % CLUSTER_FILLS.length];
  }
  return UNCLUSTERED_FILL;
}

export default function FraudRingGraph({ claimId }: FraudRingGraphProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<LoadState>({ phase: 'loading' });

  const load = useCallback(async (id: string, signal?: AbortSignal) => {
    setState({ phase: 'loading' });
    try {
      const graph = await fetchClaimGraph(id, signal);
      if (signal?.aborted) {
        return;
      }
      // A 404 (unknown claim) is treated as an unavailable graph rather than an
      // error surface — the detail view above already handles a missing claim.
      setState({
        phase: 'loaded',
        graph:
          graph ?? {
            available: false,
            reason: 'GRAPH_SERVICE_UNAVAILABLE',
            employerBn: null,
            cohortSize: 0,
            numCommunities: 0,
            clusterCount: 0,
            nodes: [],
            edges: [],
          },
      });
    } catch (err) {
      if (signal?.aborted) {
        return;
      }
      const message =
        err instanceof InvestigatorApiError || err instanceof Error
          ? err.message
          : String(err);
      setState({ phase: 'error', message });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(claimId, controller.signal);
    return () => controller.abort();
  }, [claimId, load]);

  const title = t('investigator.graph.title');

  return (
    <section aria-labelledby="fraud-ring-heading" className="panel">
      <h3 id="fraud-ring-heading" className="section-title">
        {title}
      </h3>

      {state.phase === 'loading' && (
        <p role="status" aria-live="polite">
          {t('app.loading')}
        </p>
      )}

      {state.phase === 'error' && (
        <div role="alert">
          <p>{t('investigator.graph.loadError', { message: state.message })}</p>
          <button type="button" onClick={() => void load(claimId)}>
            {t('investigator.detail.retry')}
          </button>
        </div>
      )}

      {state.phase === 'loaded' &&
        (state.graph.available ? (
          <GraphBody graph={state.graph} />
        ) : (
          <p className="graph-unavailable">
            {t(`investigator.graph.unavailable.${state.graph.reason ?? 'GRAPH_SERVICE_UNAVAILABLE'}`)}
          </p>
        ))}
    </section>
  );
}

function GraphBody({ graph }: { graph: FraudRingGraphData }) {
  const { t } = useTranslation();
  const { hub, claimants } = useMemo(
    () => layoutNodes(graph.nodes),
    [graph.nodes],
  );

  const positionById = useMemo(() => {
    const map = new Map<string, PositionedNode>();
    if (hub) {
      map.set(hub.id, hub);
    }
    for (const c of claimants) {
      map.set(c.id, c);
    }
    return map;
  }, [hub, claimants]);

  const description = t('investigator.graph.description', {
    cohort: graph.cohortSize,
    clusters: graph.clusterCount,
  });

  const edgeStroke = (edge: GraphEdge): string =>
    edge.kind === 'EMPLOYED_BY' ? '#94a3b8' : '#dc2626';

  return (
    <>
      <p className="graph-summary">{description}</p>

      <div role="img" aria-label={`${t('investigator.graph.title')}. ${description}`}>
        <svg
          className="fraud-ring-svg"
          width="100%"
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          preserveAspectRatio="xMidYMid meet"
        >
          {/* Edges first so nodes render on top. */}
          <g>
            {graph.edges.map((edge, i) => {
              const a = positionById.get(edge.source);
              const b = positionById.get(edge.target);
              if (!a || !b) {
                return null;
              }
              return (
                <line
                  key={`${edge.source}-${edge.target}-${edge.kind}-${i}`}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={edgeStroke(edge)}
                  strokeWidth={edge.kind === 'EMPLOYED_BY' ? 1 : 2}
                  strokeDasharray={edge.kind === 'SHARED_ADDRESS' ? '4 3' : undefined}
                />
              );
            })}
          </g>

          {/* Nodes. */}
          <g>
            {hub && (
              <g>
                <circle cx={hub.x} cy={hub.y} r={18} fill={nodeFill(hub)} stroke="#fff" strokeWidth={2} />
                <text x={hub.x} y={hub.y + 3} textAnchor="middle" fontSize={7} fontWeight={700} fill="#fff">
                  {t('investigator.graph.employerNode')}
                </text>
              </g>
            )}
            {claimants.map((node) => (
              <circle
                key={node.id}
                cx={node.x}
                cy={node.y}
                r={9}
                fill={nodeFill(node)}
                stroke="#fff"
                strokeWidth={node.flagged ? 1.5 : 1}
              >
                <title>{node.label}</title>
              </circle>
            ))}
          </g>
        </svg>
      </div>

      {/* Legend (visible). Colour is redundant with the labels. */}
      <ul className="graph-legend" aria-hidden="true">
        <li>
          <span className="legend-swatch" style={{ background: HUB_FILL }} />
          {t('investigator.graph.legend.employer')}
        </li>
        <li>
          <span className="legend-swatch" style={{ background: CLUSTER_FILLS[0] }} />
          {t('investigator.graph.legend.cluster')}
        </li>
        <li>
          <span className="legend-swatch" style={{ background: UNCLUSTERED_FILL }} />
          {t('investigator.graph.legend.unclustered')}
        </li>
      </ul>

      {/*
        Visually-hidden data table — the authoritative screen-reader alternative
        to the SVG. Every node is listed with its type and cluster membership so
        the meaning never relies on colour alone (WCAG 1.4.1).
      */}
      <table className="sr-only">
        <caption>{description}</caption>
        <thead>
          <tr>
            <th scope="col">{t('investigator.graph.table.node')}</th>
            <th scope="col">{t('investigator.graph.table.type')}</th>
            <th scope="col">{t('investigator.graph.table.cluster')}</th>
          </tr>
        </thead>
        <tbody>
          {graph.nodes.map((node) => (
            <tr key={node.id}>
              <th scope="row">{node.label}</th>
              <td>{t(`investigator.graph.nodeType.${node.type}`)}</td>
              <td>
                {node.type === 'EMPLOYER'
                  ? t('investigator.graph.table.hub')
                  : node.flagged
                    ? t('investigator.graph.table.inCluster', {
                        community: node.community ?? '',
                      })
                    : t('investigator.graph.table.notInCluster')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
