import { describe, expect, it } from 'vitest';

import { ROLES } from '../auth/roles';
import {
  MASKED_PLACEHOLDER,
  canViewUnmaskedPii,
  maskFlaggedClaimForRoles,
  maskSin,
} from './pii';
import type { FlaggedClaim } from './flaggedClaims';

const baseClaim: FlaggedClaim = {
  claimId: 'c-1',
  riskScore: 82,
  riskDecision: 'INVESTIGATE',
  receivedAt: '2024-01-02T03:04:05Z',
  employerBn: '123456789RP0001',
  claimantName: 'Jordan Tremblay',
  claimantSin: '123456789',
  address: '123 Main St, Ottawa ON',
};

describe('canViewUnmaskedPii', () => {
  it('grants access to INVESTIGATOR and ADMINISTRATOR', () => {
    expect(canViewUnmaskedPii([ROLES.INVESTIGATOR])).toBe(true);
    expect(canViewUnmaskedPii([ROLES.ADMINISTRATOR])).toBe(true);
  });

  it('denies access to REVIEWER-only users', () => {
    expect(canViewUnmaskedPii([ROLES.REVIEWER])).toBe(false);
  });

  it('grants access when a privileged role accompanies REVIEWER', () => {
    expect(canViewUnmaskedPii([ROLES.REVIEWER, ROLES.INVESTIGATOR])).toBe(true);
  });

  it('denies access for an empty role set', () => {
    expect(canViewUnmaskedPii([])).toBe(false);
  });
});

describe('maskSin', () => {
  it('reveals only the last three digits', () => {
    expect(maskSin('123456789')).toBe('••••••789');
  });

  it('masks short or empty values entirely', () => {
    expect(maskSin('12')).toBe(MASKED_PLACEHOLDER);
    expect(maskSin('')).toBe(MASKED_PLACEHOLDER);
    expect(maskSin(null)).toBe(MASKED_PLACEHOLDER);
  });
});

describe('maskFlaggedClaimForRoles', () => {
  it('returns the claim unchanged for privileged roles', () => {
    const masked = maskFlaggedClaimForRoles(baseClaim, [ROLES.INVESTIGATOR]);
    expect(masked).toEqual(baseClaim);
  });

  it('masks name, SIN, and address for REVIEWER while preserving risk data', () => {
    const masked = maskFlaggedClaimForRoles(baseClaim, [ROLES.REVIEWER]);
    expect(masked.claimantName).toBe(MASKED_PLACEHOLDER);
    expect(masked.address).toBe(MASKED_PLACEHOLDER);
    expect(masked.claimantSin).toBe('••••••789');
    // Non-PII fields are preserved so reviewers can still triage by risk.
    expect(masked.riskScore).toBe(baseClaim.riskScore);
    expect(masked.riskDecision).toBe(baseClaim.riskDecision);
    expect(masked.employerBn).toBe(baseClaim.employerBn);
  });

  it('never leaks raw PII for a REVIEWER across many generated inputs', () => {
    // Deterministic sweep across a range of PII values, exercising the masking
    // invariant broadly without adding a property-testing dependency.
    for (let i = 0; i < 200; i += 1) {
      const name = `Claimant Name ${i} ${'x'.repeat(i % 30)}`;
      const sin = String(100_000_000 + ((i * 7_654_321) % 899_999_999));
      const address = `${i} Elm Street, Unit ${i % 50}, City`;
      const claim: FlaggedClaim = {
        ...baseClaim,
        claimantName: name,
        claimantSin: sin,
        address,
      };
      const masked = maskFlaggedClaimForRoles(claim, [ROLES.REVIEWER]);
      expect(masked.claimantName).not.toBe(name);
      expect(masked.address).not.toBe(address);
      expect(masked.claimantSin).not.toBe(sin);
      // At most the last three SIN digits may survive.
      expect(masked.claimantSin?.endsWith(sin.slice(-3))).toBe(true);
    }
  });
});
