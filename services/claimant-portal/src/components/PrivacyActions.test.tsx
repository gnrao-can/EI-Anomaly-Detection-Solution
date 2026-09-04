import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { axe } from 'jest-axe';

import i18n from '../i18n/config';
import PrivacyActions from './PrivacyActions';

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

describe('PrivacyActions', () => {
  afterEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage('en');
  });

  it('submits a data access request immediately and announces the outcome', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ reference_id: 'REQ-A' }));

    render(<PrivacyActions />);

    fireEvent.click(screen.getByRole('button', { name: 'Request my data' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('REQ-A');
    });
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/privacy/access');
    expect((init as RequestInit).method).toBe('GET');
  });

  it('requires confirmation before firing a deletion request', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ reference_id: 'REQ-D' }));

    render(<PrivacyActions />);

    // First click only reveals the confirmation — no network call yet.
    fireEvent.click(
      screen.getByRole('button', { name: 'Request deletion of my data' }),
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(
      screen.getByText(/This action cannot be undone/i),
    ).toBeInTheDocument();

    // Confirming fires the DELETE and announces the reference.
    fireEvent.click(
      screen.getByRole('button', { name: 'Yes, request deletion' }),
    );
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('REQ-D');
    });
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/privacy/delete');
    expect((init as RequestInit).method).toBe('DELETE');
  });

  it('cancelling the deletion confirmation makes no request', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    render(<PrivacyActions />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Request deletion of my data' }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(
      screen.queryByText(/This action cannot be undone/i),
    ).not.toBeInTheDocument();
  });

  it('announces an error when a request fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}, 500));

    render(<PrivacyActions />);
    fireEvent.click(screen.getByRole('button', { name: 'Request my data' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/could not be/i);
    });
  });

  it('has no detectable WCAG violations (axe)', async () => {
    const { container } = render(<PrivacyActions />);
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });
});
