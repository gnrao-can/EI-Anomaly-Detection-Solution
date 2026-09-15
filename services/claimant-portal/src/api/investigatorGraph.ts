import { InvestigatorApiError } from './investigatorClaim';

/**
 * Fraud-ring network graph data access for the investigator detail view
 * (Req 22.2 surface; data from the Req 5.1 bipartite claimant/employer graph +
 * Req 5.5 community clustering).
 *
 * The portal backend composes this graph LIVE from the Graph Service — there is
 * no persisted node/edge store — so the endpoint degrades HONESTLY: when the
 * claim has no employer BN or the Graph Service is unavailable/empty, it returns
 * `available: false` with a `reason` and an empty node/edge set. The UI then
 * shows a "no network data" state rather than a fabricated ring.
 *
 * Endpoint: `GET /portal/investigator/claims/{claim_id}/graph`
 * (INVESTIGATOR / ADMINISTRATOR only — Req 22.6). Node identifiers are
 * pseudonymous (never raw PII).
 */

/** A node in the fraud-ring network. */
export type GraphNodeType = 'EMPLOYER' | 'CLAIMANT';

export interface GraphNode {
  id: string;
  type: GraphNodeType;
  label: string;
  /** Detected community id (null for the employer hub or an unpartitioned node). */
  community: number | null;
  /** True when the node belongs to a connected cluster (community size >= 2). */
  flagged: boolean;
  postalFsa?: string | null;
  insurableHours?: number | null;
  separationDate?: string | null;
  claimId?: string | null;
}

/** The kind of relationship an edge represents. */
export type GraphEdgeKind =
  | 'EMPLOYED_BY'
  | 'SHARED_SURNAME'
  | 'SHARED_ADDRESS';

export interface GraphEdge {
  source: string;
  target: string;
  kind: GraphEdgeKind;
}

/** Reason the graph is unavailable, when `available` is false. */
export type GraphUnavailableReason =
  | 'NO_EMPLOYER'
  | 'GRAPH_SERVICE_UNAVAILABLE'
  | 'EMPTY_COHORT';

/** The composed fraud-ring network for a claim. */
export interface FraudRingGraph {
  available: boolean;
  reason: GraphUnavailableReason | null;
  employerBn: string | null;
  cohortSize: number;
  numCommunities: number;
  clusterCount: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

const NODE_TYPES: readonly GraphNodeType[] = ['EMPLOYER', 'CLAIMANT'];
const EDGE_KINDS: readonly GraphEdgeKind[] = [
  'EMPLOYED_BY',
  'SHARED_SURNAME',
  'SHARED_ADDRESS',
];

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function parseNode(payload: unknown): GraphNode | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const r = payload as Record<string, unknown>;
  const id = r.id;
  if (typeof id !== 'string' || id.length === 0) {
    return null;
  }
  const type = (NODE_TYPES as readonly string[]).includes(r.type as string)
    ? (r.type as GraphNodeType)
    : 'CLAIMANT';
  return {
    id,
    type,
    label: optionalString(r.label) ?? id,
    community: optionalNumber(r.community),
    flagged: r.flagged === true,
    postalFsa: optionalString(r.postal_fsa ?? r.postalFsa),
    insurableHours: optionalNumber(r.insurable_hours ?? r.insurableHours),
    separationDate: optionalString(r.separation_date ?? r.separationDate),
    claimId: optionalString(r.claim_id ?? r.claimId),
  };
}

function parseEdge(payload: unknown): GraphEdge | null {
  if (payload == null || typeof payload !== 'object') {
    return null;
  }
  const r = payload as Record<string, unknown>;
  const source = r.source;
  const target = r.target;
  if (typeof source !== 'string' || typeof target !== 'string') {
    return null;
  }
  const kind = (EDGE_KINDS as readonly string[]).includes(r.kind as string)
    ? (r.kind as GraphEdgeKind)
    : 'EMPLOYED_BY';
  return { source, target, kind };
}

function isUnavailableReason(
  value: unknown,
): value is GraphUnavailableReason {
  return (
    value === 'NO_EMPLOYER' ||
    value === 'GRAPH_SERVICE_UNAVAILABLE' ||
    value === 'EMPTY_COHORT'
  );
}

/**
 * Parse a raw graph payload defensively. Unknown/malformed input yields an
 * honest empty, unavailable graph rather than throwing, so the view can render
 * a "no network data" state.
 */
export function parseFraudRingGraph(payload: unknown): FraudRingGraph {
  const empty: FraudRingGraph = {
    available: false,
    reason: 'GRAPH_SERVICE_UNAVAILABLE',
    employerBn: null,
    cohortSize: 0,
    numCommunities: 0,
    clusterCount: 0,
    nodes: [],
    edges: [],
  };
  if (payload == null || typeof payload !== 'object') {
    return empty;
  }
  const r = payload as Record<string, unknown>;
  const nodes = Array.isArray(r.nodes)
    ? r.nodes.map(parseNode).filter((n): n is GraphNode => n !== null)
    : [];
  const edges = Array.isArray(r.edges)
    ? r.edges.map(parseEdge).filter((e): e is GraphEdge => e !== null)
    : [];
  const reasonRaw = r.reason;
  return {
    available: r.available === true,
    reason: isUnavailableReason(reasonRaw) ? reasonRaw : null,
    employerBn: optionalString(r.employer_bn ?? r.employerBn),
    cohortSize: optionalNumber(r.cohort_size ?? r.cohortSize) ?? 0,
    numCommunities: optionalNumber(r.num_communities ?? r.numCommunities) ?? 0,
    clusterCount: optionalNumber(r.cluster_count ?? r.clusterCount) ?? 0,
    nodes,
    edges,
  };
}

/**
 * Fetch the fraud-ring network graph for a claim. The HttpOnly session cookie
 * authenticates the request. Returns `null` when the claim is not found (404);
 * a non-OK status other than 404 throws an {@link InvestigatorApiError}.
 */
export async function fetchClaimGraph(
  claimId: string,
  signal?: AbortSignal,
): Promise<FraudRingGraph | null> {
  const response = await fetch(
    `/portal/investigator/claims/${encodeURIComponent(claimId)}/graph`,
    {
      method: 'GET',
      credentials: 'include',
      headers: { Accept: 'application/json' },
      signal,
    },
  );

  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new InvestigatorApiError(
      response.status,
      response.statusText || `HTTP ${response.status}`,
    );
  }

  const payload: unknown = await response.json();
  return parseFraudRingGraph(payload);
}
