import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { axe } from 'jest-axe';

import '../i18n/config';
import HomePage from './HomePage';

describe('HomePage accessibility', () => {
  it('has no detectable WCAG violations (axe)', async () => {
    const { container } = render(<HomePage />);
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });
});
