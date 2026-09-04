import { describe, expect, it } from 'vitest';

import {
  DEFAULT_LANGUAGE,
  normaliseLanguage,
  SUPPORTED_LANGUAGES,
} from './config';

describe('normaliseLanguage', () => {
  it('accepts supported base languages regardless of case', () => {
    expect(normaliseLanguage('en')).toBe('en');
    expect(normaliseLanguage('FR')).toBe('fr');
  });

  it('strips region subtags (e.g. fr-CA -> fr)', () => {
    expect(normaliseLanguage('fr-CA')).toBe('fr');
    expect(normaliseLanguage('en-US')).toBe('en');
  });

  it('falls back to the default for unsupported or empty values', () => {
    expect(normaliseLanguage('es')).toBe(DEFAULT_LANGUAGE);
    expect(normaliseLanguage('')).toBe(DEFAULT_LANGUAGE);
    expect(normaliseLanguage(null)).toBe(DEFAULT_LANGUAGE);
    expect(normaliseLanguage(undefined)).toBe(DEFAULT_LANGUAGE);
  });

  it('only ever returns a supported language', () => {
    const samples = ['en', 'fr', 'fr-CA', 'EN-us', 'de', 'zz', '', 'FrEnCh'];
    for (const sample of samples) {
      expect(SUPPORTED_LANGUAGES).toContain(normaliseLanguage(sample));
    }
  });
});
