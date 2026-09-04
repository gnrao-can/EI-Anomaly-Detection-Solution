import { afterEach, describe, expect, it, vi } from 'vitest';

import { ROLES } from '../auth/roles';
import { MASKED_PLACEHOLDER } from './pii';
import {
  fetchFlaggedClaims,
  isFlaggedDecision,
  mergeFlaggedClaim,
  parseFlaggedClaim,
  sortByRiskScoreDescending,
  type FlaggedClaim,
} from './flaggedClaims';

function claim(overrides: Partial<FlaggedClaim> = {}): FlaggedClaim {
  return {
    claimId: 'c-1',
    riskScore: 50,
    riskDecision: 'FLAG_FOR_REVIEW',
    receivedAt: '2024-01-01T00:00:00Z',
    employerBn: 'BN1',
    claimantName: 'A',
    claimantSin: '123456789',
    address: 'addr',
    ...overrides,
  };
}

describe('isFlaggedDecision', () => {
  it('accepts only the two flagged decisions', () => {
    expect(isFlaggedDecision('FLAG_FOR_REVIEW')).toBe(true);
    expect(isFlaggedDecision('INVESTIGATE')).toBe(true);
    expect(isFlaggedDecision('AUTO_APPROVE')).toBe(false);
    expect(isFlaggedDecision('INDETERMINATE')).toBe(false);
    expect(isFlaggedDecision(undefined)).toBe(false);
  });
});

describe('parseFlaggedClaim', () => {
  it('parses a snake_case backend row and clamps the score', () => {
    const parsed = parseFlaggedClaim({
      claim_id: 'abc',
      risk_score: 140,
      risk_decision: 'INVESTIGATE',
      received_at: '2024-05-01T12:00:00Z',
      employer_bn: 'BN9',
      claimant_name: 'Jane',
    });
    expect(parsed).not.toBeNull();
    expect(parsed?.claimId).toBe('abc');
    expect(parsed?.riskScore).toBe(100); // clamped into [0, 100]
    expect(parsed?.riskDecision).toBe('INVESTIGATE');
  });

  it('rejects rows that are not a flagged decision', () => {
    expect(
      parseFlaggedClaim({
        claim_id: 'x',
        risk_score: 10,
        risk_decision: 'AUTO_APPROVE',
      }),
    ).toBeNull();
  });

  it('rejects malformed rows', () => {
    expect(parseFlaggedClaim(null)).toBeNull();
    expect(parseFlaggedClaim({ risk_score: 5 })).toBeNull();
    expect(
      parseFlaggedClaim({ claim_id: 'x', risk_decision: 'INVESTIGATE' }),
    ).toBeNull();
  });
});

describe('sortByRiskScoreDescending', () => {
  it('orders by score descending, then most recent first', () => {
    const sorted = sortByRiskScoreDescending([
      claim({ claimId: 'a', riskScore: 40, receivedAt: '2024-01-01T00:00:00Z' }),
      claim({ claimId: 'b', riskScore: 90, receivedAt: '2024-01-01T00:00:00Z' }),
      claim({ claimId: 'c', riskScore: 90, receivedAt: '2024-02-01T00:00:00Z' }),
    ]);
    expect(sorted.map((c) => c.claimId)).toEqual(['c', 'b', 'a']);
  });

  it('is non-mutating and idempotent', () => {
    const input = [claim({ claimId: 'a', riskScore: 10 }), claim({ claimId: 'b', riskScore: 20 })];
    const once = sortByRiskScoreDescending(input);
    const twice = sortByRiskScoreDescending(once);
    expect(twice).toEqual(once);
    expect(input.map((c) => c.claimId)).toEqual(['a', 'b']); // unchanged
  });
});

describe('mergeFlaggedClaim', () => {
  it('inserts a new claim in sorted position', () => {
    const existing = [claim({ claimId: 'a', riskScore: 80 })];
    const merged = mergeFlaggedClaim(existing, claim({ claimId: 'b', riskScore: 95 }));
    expect(merged.map((c) => c.claimId)).toEqual(['b', 'a']);
  });

  it('replaces an existing claim with the same id (de-dup)', () => {
    const existing = [
      claim({ claimId: 'a', riskScore: 80 }),
      claim({ claimId: 'b', riskScore: 40 }),
    ];
    const merged = mergeFlaggedClaim(existing, claim({ claimId: 'b', riskScore: 99 }));
    expect(merged).toHaveLength(2);
    expect(merged.map((c) => c.claimId)).toEqual(['b', 'a']);
    expect(merged[0].riskScore).toBe(99);
  });
});

describe('fetchFlaggedClaims', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetches, sorts, and masks PII for a REVIEWER', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => [
        {
          claim_id: 'low',
          risk_score: 35,
          risk_decision: 'FLAG_FOR_REVIEW',
          claimant_name: 'Secret Person',
          claimant_sin: '987654321',
        },
        {
          claim_id: 'high',
          risk_score: 88,
          risk_decision: 'INVESTIGATE',
          claimant_name: 'Another Person',
          claimant_sin: '111222333',
        },
      ],
    } as Response);

    const rows = await fetchFlaggedClaims([ROLES.REVIEWER]);
    expect(rows.map((c) => c.claimId)).toEqual(['high', 'low']); // sorted desc
    expect(rows[0].claimantName).toBe(MASKED_PLACEHOLDER);
    expect(rows[0].claimantSin).toBe('••••••333');
  });

  it('returns unmasked data for an INVESTIGATOR', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        claims: [
          {
            claim_id: 'x',
            risk_score: 70,
            risk_decision: 'INVESTIGATE',
            claimant_name: 'Real Name',
            claimant_sin: '123123123',
          },
        ],
      }),
    } as Response);

    const rows = await fetchFlaggedClaims([ROLES.INVESTIGATOR]);
    expect(rows[0].claimantName).toBe('Real Name');
    expect(rows[0].claimantSin).toBe('123123123');
  });

  it('throws on a non-OK response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({}),
    } as Response);

    await expect(fetchFlaggedClaims([ROLES.REVIEWER])).rejects.toThrow(/403/);
  });
});
