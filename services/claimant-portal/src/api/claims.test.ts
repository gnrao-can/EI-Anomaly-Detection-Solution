import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  fetchClaimStatus,
  parseClaimStatus,
  requestDataAccess,
  requestDataDeletion,
  selectExplanation,
} from './claims';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
    clone() {
      return this as unknown as Response;
    },
  } as Response;
}

describe('parseClaimStatus', () => {
  it('parses a well-formed snake_case payload', () => {
    const view = parseClaimStatus({
      claim_id: 'c-1',
      status: 'DECIDED',
      risk_decision: 'AUTO_APPROVE',
      risk_score: 12,
      explanation_en: 'No concerns found.',
      explanation_fr: 'Aucune préoccupation.',
    });
    expect(view).toEqual({
      claimId: 'c-1',
      status: 'DECIDED',
      riskDecision: 'AUTO_APPROVE',
      riskScore: 12,
      explanationEn: 'No concerns found.',
      explanationFr: 'Aucune préoccupation.',
      submittedAt: null,
      decidedAt: null,
    });
  });

  it('nulls out an unrecognised risk decision but keeps the claim', () => {
    const view = parseClaimStatus({
      claim_id: 'c-2',
      status: 'PROCESSING',
      risk_decision: 'BOGUS',
    });
    expect(view?.riskDecision).toBeNull();
    expect(view?.status).toBe('PROCESSING');
  });

  it('returns null for payloads missing an id or with an invalid status', () => {
    expect(parseClaimStatus({ status: 'DECIDED' })).toBeNull();
    expect(parseClaimStatus({ claim_id: 'c-3', status: 'NOPE' })).toBeNull();
    expect(parseClaimStatus(null)).toBeNull();
    expect(parseClaimStatus('nope')).toBeNull();
  });
});

describe('selectExplanation', () => {
  const both = { explanationEn: 'English text', explanationFr: 'Texte français' };

  it('returns the language-matching explanation', () => {
    expect(selectExplanation(both, 'en')).toBe('English text');
    expect(selectExplanation(both, 'fr')).toBe('Texte français');
  });

  it('falls back to the other language when the preferred one is absent', () => {
    expect(
      selectExplanation({ explanationEn: 'only en', explanationFr: null }, 'fr'),
    ).toBe('only en');
    expect(
      selectExplanation({ explanationEn: null, explanationFr: 'que fr' }, 'en'),
    ).toBe('que fr');
  });

  it('returns null when neither language is present', () => {
    expect(
      selectExplanation({ explanationEn: null, explanationFr: null }, 'en'),
    ).toBeNull();
  });
});

describe('fetchClaimStatus', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('requests the claim endpoint with the session cookie', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        jsonResponse({ claim_id: 'c 1', status: 'RECEIVED' }),
      );

    const view = await fetchClaimStatus('c 1');

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/portal/claims/c%201');
    expect((init as RequestInit).credentials).toBe('include');
    expect(view?.claimId).toBe('c 1');
  });

  it('returns null on 404', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(null, 404));
    expect(await fetchClaimStatus('missing')).toBeNull();
  });

  it('throws ApiError on other non-OK statuses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}, 500));
    await expect(fetchClaimStatus('c-1')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('privacy requests', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('requestDataAccess issues a GET to the access endpoint', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ reference_id: 'REQ-1' }));

    const result = await requestDataAccess();

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/privacy/access');
    expect((init as RequestInit).method).toBe('GET');
    expect(result.referenceId).toBe('REQ-1');
  });

  it('requestDataDeletion issues a DELETE to the delete endpoint', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ reference_id: 'REQ-2' }));

    const result = await requestDataDeletion();

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/privacy/delete');
    expect((init as RequestInit).method).toBe('DELETE');
    expect(result.referenceId).toBe('REQ-2');
  });

  it('throws ApiError when a privacy request fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}, 403));
    await expect(requestDataDeletion()).rejects.toBeInstanceOf(ApiError);
  });
});
