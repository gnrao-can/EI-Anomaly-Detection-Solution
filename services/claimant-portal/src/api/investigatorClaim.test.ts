import { afterEach, describe, expect, it, vi } from 'vitest';

import { ROLES } from '../auth/roles';
import { MASKED_PLACEHOLDER } from './pii';
import {
  fetchInvestigatorClaimDetail,
  isDisposition,
  maskClaimDetailForRoles,
  parseIndicator,
  parseInvestigatorClaimDetail,
  parseShapAttribution,
  selectBilingual,
  submitDisposition,
  type InvestigatorClaimDetail,
} from './investigatorClaim';

const RAW_DETAIL = {
  claim_id: 'claim-1',
  risk_score: 87,
  risk_decision: 'INVESTIGATE',
  shap_base_value: 10.5,
  shap_attributions: [
    { indicator_id: 'ind-1', feature_name: 'geo_distance', contribution: 30.2 },
    { indicator_id: 'ind-2', feature_name: 'roe_font', contribution: -5.1 },
  ],
  indicators: [
    {
      indicator_id: 'ind-1',
      engine: 'GEOGRAPHIC',
      severity: 'CRITICAL',
      title: 'Economically implausible commute',
      explanation_en: 'Transport cost exceeds benefit.',
      explanation_fr: "Le coût de transport dépasse la prestation.",
      shap_contribution: 30.2,
    },
    {
      indicator_id: 'ind-2',
      engine: 'DOCUMENT_FORGERY',
      severity: 'MEDIUM',
      title: 'Unusual ROE font',
      explanation_en: 'Font differs from prior ROEs.',
      explanation_fr: 'La police diffère des RE antérieurs.',
      shap_contribution: -5.1,
    },
  ],
  explanation_en: 'This claim was flagged for geographic anomalies.',
  explanation_fr: "Cette demande a été signalée pour des anomalies géographiques.",
  claimant_name: 'Jordan Tremblay',
  claimant_sin: '123456789',
  employer_bn: 'BN-123',
};

describe('parseInvestigatorClaimDetail', () => {
  it('parses a well-formed snake_case payload', () => {
    const parsed = parseInvestigatorClaimDetail(RAW_DETAIL);
    expect(parsed).not.toBeNull();
    expect(parsed?.claimId).toBe('claim-1');
    expect(parsed?.riskScore).toBe(87);
    expect(parsed?.riskDecision).toBe('INVESTIGATE');
    expect(parsed?.shapAttributions).toHaveLength(2);
    expect(parsed?.indicators).toHaveLength(2);
    expect(parsed?.explanationFr).toContain('anomalies');
  });

  it('clamps risk score into [0, 100]', () => {
    expect(
      parseInvestigatorClaimDetail({ ...RAW_DETAIL, risk_score: 250 })?.riskScore,
    ).toBe(100);
    expect(
      parseInvestigatorClaimDetail({ ...RAW_DETAIL, risk_score: -7 })?.riskScore,
    ).toBe(0);
  });

  it('returns null when required fields are missing or invalid', () => {
    expect(parseInvestigatorClaimDetail(null)).toBeNull();
    expect(parseInvestigatorClaimDetail({})).toBeNull();
    expect(
      parseInvestigatorClaimDetail({ ...RAW_DETAIL, risk_decision: 'BOGUS' }),
    ).toBeNull();
    expect(
      parseInvestigatorClaimDetail({ ...RAW_DETAIL, risk_score: 'x' }),
    ).toBeNull();
  });

  it('drops malformed indicators and attributions defensively', () => {
    const parsed = parseInvestigatorClaimDetail({
      ...RAW_DETAIL,
      indicators: [{ engine: 'GEO' }, ...RAW_DETAIL.indicators],
      shap_attributions: [
        { indicator_id: 'x', contribution: 'nope' },
        ...RAW_DETAIL.shap_attributions,
      ],
    });
    expect(parsed?.indicators).toHaveLength(2);
    expect(parsed?.shapAttributions).toHaveLength(2);
  });
});

describe('parseIndicator', () => {
  it('defaults an unknown severity to UNRESOLVED', () => {
    const ind = parseIndicator({ indicator_id: 'i', severity: 'WEIRD' });
    expect(ind?.severity).toBe('UNRESOLVED');
  });

  it('returns null without an indicator id', () => {
    expect(parseIndicator({ severity: 'HIGH' })).toBeNull();
  });
});

describe('parseShapAttribution', () => {
  it('rejects a non-numeric contribution', () => {
    expect(
      parseShapAttribution({ indicator_id: 'i', contribution: 'x' }),
    ).toBeNull();
  });
});

describe('selectBilingual', () => {
  it('prefers the active language and falls back to the other', () => {
    expect(selectBilingual('EN', 'FR', 'fr')).toBe('FR');
    expect(selectBilingual('EN', 'FR', 'en')).toBe('EN');
    expect(selectBilingual(null, 'FR', 'en')).toBe('FR');
    expect(selectBilingual('EN', null, 'fr')).toBe('EN');
    expect(selectBilingual(null, null, 'en')).toBeNull();
  });
});

describe('maskClaimDetailForRoles', () => {
  const detail = parseInvestigatorClaimDetail(RAW_DETAIL) as InvestigatorClaimDetail;

  it('leaves PII intact for an INVESTIGATOR', () => {
    const masked = maskClaimDetailForRoles(detail, [ROLES.INVESTIGATOR]);
    expect(masked.claimantName).toBe('Jordan Tremblay');
    expect(masked.claimantSin).toBe('123456789');
  });

  it('masks name and SIN for a REVIEWER while keeping risk data', () => {
    const masked = maskClaimDetailForRoles(detail, [ROLES.REVIEWER]);
    expect(masked.claimantName).toBe(MASKED_PLACEHOLDER);
    expect(masked.claimantSin).toContain('789');
    expect(masked.claimantSin).not.toBe('123456789');
    expect(masked.riskScore).toBe(87);
    expect(masked.indicators).toHaveLength(2);
  });
});

describe('isDisposition', () => {
  it('recognises valid dispositions only', () => {
    expect(isDisposition('CONFIRMED_FRAUD')).toBe(true);
    expect(isDisposition('FALSE_POSITIVE')).toBe(true);
    expect(isDisposition('NEEDS_FURTHER_REVIEW')).toBe(true);
    expect(isDisposition('MAYBE')).toBe(false);
  });
});

describe('fetchInvestigatorClaimDetail', () => {
  afterEach(() => vi.restoreAllMocks());

  it('returns a masked detail for a REVIEWER', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => RAW_DETAIL,
    } as Response);

    const detail = await fetchInvestigatorClaimDetail('claim-1', [
      ROLES.REVIEWER,
    ]);
    expect(detail?.claimantName).toBe(MASKED_PLACEHOLDER);
  });

  it('returns null on a 404', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    } as Response);
    const detail = await fetchInvestigatorClaimDetail('missing', [
      ROLES.INVESTIGATOR,
    ]);
    expect(detail).toBeNull();
  });

  it('throws on a non-OK, non-404 response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Server Error',
      clone: () => ({ json: async () => ({}) }),
      json: async () => ({}),
    } as unknown as Response);
    await expect(
      fetchInvestigatorClaimDetail('claim-1', [ROLES.INVESTIGATOR]),
    ).rejects.toThrow();
  });
});

describe('submitDisposition', () => {
  afterEach(() => vi.restoreAllMocks());

  it('PUTs the disposition to the correct endpoint with snake_case body', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({}) } as Response);

    await submitDisposition('claim-9', {
      disposition: 'CONFIRMED_FRAUD',
      caseNotes: 'Confirmed via CRA cross-check.',
    });

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/claims/claim-9/disposition');
    expect(init?.method).toBe('PUT');
    const body = JSON.parse((init?.body as string) ?? '{}');
    expect(body.disposition).toBe('CONFIRMED_FRAUD');
    expect(body.case_notes).toBe('Confirmed via CRA cross-check.');
  });

  it('throws InvestigatorApiError on failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 403,
      statusText: 'Forbidden',
      clone: () => ({ json: async () => ({}) }),
      json: async () => ({}),
    } as unknown as Response);
    await expect(
      submitDisposition('c', { disposition: 'FALSE_POSITIVE', caseNotes: '' }),
    ).rejects.toThrow();
  });
});
