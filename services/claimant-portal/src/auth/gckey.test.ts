import { describe, expect, it } from 'vitest';

import { parseSession } from './gckey';
import { ROLES } from './roles';

describe('parseSession', () => {
  it('parses a well-formed session payload', () => {
    const session = parseSession({
      subject: 'gckey-sub-123',
      roles: ['INVESTIGATOR', 'ADMINISTRATOR'],
      preferredLanguage: 'fr',
      displayName: 'A. Investigator',
    });
    expect(session).toEqual({
      subject: 'gckey-sub-123',
      roles: [ROLES.INVESTIGATOR, ROLES.ADMINISTRATOR],
      preferredLanguage: 'fr',
      displayName: 'A. Investigator',
    });
  });

  it('drops unknown roles and tolerates missing optional fields', () => {
    const session = parseSession({
      subject: 'sub',
      roles: ['CLAIMANT', 'ROOT', 42],
    });
    expect(session?.roles).toEqual([ROLES.CLAIMANT]);
    expect(session?.preferredLanguage).toBeNull();
    expect(session?.displayName).toBeUndefined();
  });

  it('returns null for payloads without a valid subject', () => {
    expect(parseSession(null)).toBeNull();
    expect(parseSession(undefined)).toBeNull();
    expect(parseSession({})).toBeNull();
    expect(parseSession({ subject: '' })).toBeNull();
    expect(parseSession('not-an-object')).toBeNull();
  });
});
