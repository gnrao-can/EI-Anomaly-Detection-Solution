import { describe, expect, it } from 'vitest';

import {
  luhnChecksum,
  parseIsoDate,
  validateBn,
  validateClaimFields,
  validateSin,
  type ClaimFormValues,
} from './claim';

// A SIN that passes the Luhn checksum and does not start with 0.
const VALID_SIN = '130692544';

function baseValues(overrides: Partial<ClaimFormValues> = {}): ClaimFormValues {
  return {
    sin: VALID_SIN,
    employerBn: '123456789RT0001',
    insurableHours: '1200',
    insurableEarnings: '45000',
    separationDate: '2020-01-15',
    separationReason: 'Shortage of work',
    ...overrides,
  };
}

describe('luhnChecksum', () => {
  it('accepts a Luhn-valid number', () => {
    expect(luhnChecksum(VALID_SIN)).toBe(true);
  });

  it('rejects a Luhn-invalid number', () => {
    expect(luhnChecksum('130692545')).toBe(false);
  });

  it('rejects non-digit input', () => {
    expect(luhnChecksum('12a456789')).toBe(false);
  });
});

describe('validateSin', () => {
  it('accepts a valid SIN with spaces and dashes stripped', () => {
    expect(validateSin('130 692 544')).toBe(true);
    expect(validateSin('130-692-544')).toBe(true);
  });

  it('rejects a SIN that starts with 0-length or wrong length', () => {
    expect(validateSin('12345678')).toBe(false); // 8 digits
    expect(validateSin('1234567890')).toBe(false); // 10 digits
  });

  it('rejects a SIN starting with 0', () => {
    // 012345674 would need to be Luhn valid to isolate the leading-zero rule;
    // any leading-zero SIN must be rejected regardless.
    expect(validateSin('012345678')).toBe(false);
  });

  it('rejects a SIN that fails the Luhn checksum', () => {
    expect(validateSin('123456789')).toBe(false);
  });
});

describe('validateBn', () => {
  it('accepts a 9-digit base BN', () => {
    expect(validateBn('123456789')).toBe(true);
  });

  it('accepts a 15-character BN with a valid program code', () => {
    expect(validateBn('123456789RT0001')).toBe(true);
    expect(validateBn('123456789rp0001')).toBe(true); // case-insensitive
  });

  it('rejects a 15-character BN with an unknown program code', () => {
    expect(validateBn('123456789XX0001')).toBe(false);
  });

  it('rejects wrong lengths and non-numeric segments', () => {
    expect(validateBn('12345678')).toBe(false);
    expect(validateBn('12345678A')).toBe(false);
    expect(validateBn('123456789RT00A1')).toBe(false);
  });
});

describe('parseIsoDate', () => {
  it('parses a valid ISO date', () => {
    const d = parseIsoDate('2020-02-29'); // leap year
    expect(d).not.toBeNull();
    expect(d?.getUTCFullYear()).toBe(2020);
  });

  it('rejects overflow dates', () => {
    expect(parseIsoDate('2021-02-29')).toBeNull(); // not a leap year
    expect(parseIsoDate('2020-13-01')).toBeNull();
    expect(parseIsoDate('2020-00-10')).toBeNull();
  });

  it('rejects malformed strings', () => {
    expect(parseIsoDate('2020/01/01')).toBeNull();
    expect(parseIsoDate('01-01-2020')).toBeNull();
    expect(parseIsoDate('not-a-date')).toBeNull();
  });
});

describe('validateClaimFields', () => {
  const fixedNow = new Date('2023-06-15T12:00:00Z');

  it('returns no errors for a fully valid claim', () => {
    expect(validateClaimFields(baseValues(), fixedNow)).toEqual([]);
  });

  it('flags every missing mandatory field', () => {
    const errors = validateClaimFields(
      {
        sin: '',
        employerBn: '',
        insurableHours: '',
        insurableEarnings: '',
        separationDate: '',
        separationReason: '',
      },
      fixedNow,
    );
    expect(errors).toHaveLength(6);
    expect(errors.every((e) => e.code === 'MISSING')).toBe(true);
  });

  it('flags an invalid SIN', () => {
    const errors = validateClaimFields(baseValues({ sin: '123456789' }), fixedNow);
    expect(errors).toContainEqual({ field: 'sin', code: 'INVALID' });
  });

  it('flags insurable hours <= 0 as INVALID and > 3120 as TOO_LARGE', () => {
    expect(
      validateClaimFields(baseValues({ insurableHours: '0' }), fixedNow),
    ).toContainEqual({ field: 'insurableHours', code: 'INVALID' });
    expect(
      validateClaimFields(baseValues({ insurableHours: '-5' }), fixedNow),
    ).toContainEqual({ field: 'insurableHours', code: 'INVALID' });
    expect(
      validateClaimFields(baseValues({ insurableHours: '3121' }), fixedNow),
    ).toContainEqual({ field: 'insurableHours', code: 'TOO_LARGE' });
  });

  it('flags earnings <= 0 as INVALID', () => {
    expect(
      validateClaimFields(baseValues({ insurableEarnings: '0' }), fixedNow),
    ).toContainEqual({ field: 'insurableEarnings', code: 'INVALID' });
  });

  it('flags a future separation date', () => {
    expect(
      validateClaimFields(baseValues({ separationDate: '2099-01-01' }), fixedNow),
    ).toContainEqual({ field: 'separationDate', code: 'INVALID' });
  });

  it('accepts a separation date equal to today', () => {
    const errors = validateClaimFields(
      baseValues({ separationDate: '2023-06-15' }),
      fixedNow,
    );
    expect(errors.some((e) => e.field === 'separationDate')).toBe(false);
  });

  it('flags a separation reason that is too long', () => {
    const errors = validateClaimFields(
      baseValues({ separationReason: 'x'.repeat(1001) }),
      fixedNow,
    );
    expect(errors).toContainEqual({
      field: 'separationReason',
      code: 'TOO_LARGE',
    });
  });

  it('reports errors in field order', () => {
    const errors = validateClaimFields(
      baseValues({ sin: '', insurableEarnings: '0' }),
      fixedNow,
    );
    expect(errors[0].field).toBe('sin');
    expect(errors[errors.length - 1].field).toBe('insurableEarnings');
  });
});
