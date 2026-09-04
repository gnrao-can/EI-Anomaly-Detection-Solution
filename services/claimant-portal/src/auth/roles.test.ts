import { describe, expect, it } from 'vitest';

import { ALL_ROLES, hasAnyRole, isRole, ROLES } from './roles';

describe('roles', () => {
  it('recognises all defined roles', () => {
    for (const role of ALL_ROLES) {
      expect(isRole(role)).toBe(true);
    }
  });

  it('rejects unknown or malformed role values', () => {
    expect(isRole('ROOT')).toBe(false);
    expect(isRole('')).toBe(false);
    expect(isRole(null)).toBe(false);
    expect(isRole(42)).toBe(false);
    expect(isRole(undefined)).toBe(false);
  });

  describe('hasAnyRole', () => {
    it('permits any authenticated user when no roles are required', () => {
      expect(hasAnyRole([], [])).toBe(true);
      expect(hasAnyRole([ROLES.CLAIMANT], [])).toBe(true);
    });

    it('grants access when the user holds a required role', () => {
      expect(hasAnyRole([ROLES.INVESTIGATOR], [ROLES.INVESTIGATOR])).toBe(true);
      expect(
        hasAnyRole(
          [ROLES.REVIEWER],
          [ROLES.REVIEWER, ROLES.INVESTIGATOR, ROLES.ADMINISTRATOR],
        ),
      ).toBe(true);
    });

    it('denies access when the user lacks every required role', () => {
      expect(hasAnyRole([ROLES.CLAIMANT], [ROLES.INVESTIGATOR])).toBe(false);
      expect(hasAnyRole([], [ROLES.ADMINISTRATOR])).toBe(false);
    });
  });
});
