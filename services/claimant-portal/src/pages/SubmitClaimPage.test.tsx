import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { axe } from 'jest-axe';

import i18n from '../i18n/config';
import SubmitClaimPage from './SubmitClaimPage';

const VALID_SIN = '130692544';

/** Fill every mandatory field with a valid value. */
function fillValidForm() {
  fireEvent.change(screen.getByLabelText(/Social Insurance Number/i), {
    target: { value: VALID_SIN },
  });
  fireEvent.change(screen.getByLabelText(/Business Number/i), {
    target: { value: '123456789RT0001' },
  });
  fireEvent.change(screen.getByLabelText(/Insurable hours/i), {
    target: { value: '1200' },
  });
  fireEvent.change(screen.getByLabelText(/Insurable earnings/i), {
    target: { value: '45000' },
  });
  fireEvent.change(screen.getByLabelText(/Separation date/i), {
    target: { value: '2020-01-15' },
  });
  fireEvent.change(screen.getByLabelText(/Reason for separation/i), {
    target: { value: 'Shortage of work' },
  });
}

describe('SubmitClaimPage', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('has no detectable WCAG violations (axe)', async () => {
    const { container } = render(<SubmitClaimPage />);
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });

  it('renders accessible labels tied to inputs for every mandatory field', () => {
    render(<SubmitClaimPage />);
    expect(screen.getByLabelText(/Social Insurance Number/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Business Number/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Insurable hours/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Insurable earnings/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Separation date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Reason for separation/i)).toBeInTheDocument();
  });

  it('announces validation errors via role="alert" and marks fields invalid', async () => {
    render(<SubmitClaimPage />);
    fireEvent.click(screen.getByRole('button', { name: /Submit claim/i }));

    // Error summary alert appears.
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.length).toBeGreaterThan(0);

    const sin = screen.getByLabelText(/Social Insurance Number/i);
    expect(sin).toHaveAttribute('aria-invalid', 'true');
    // aria-describedby links hint + error ids.
    expect(sin.getAttribute('aria-describedby')).toContain('sin-error');
  });

  it('does not submit to the server when client validation fails', () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('{}', { status: 200 }));
    render(<SubmitClaimPage />);
    fireEvent.click(screen.getByRole('button', { name: /Submit claim/i }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('shows a confirmation with the Claim ID on success', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ claim_id: 'CLM-123' }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    render(<SubmitClaimPage />);
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: /Submit claim/i }));

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('CLM-123'),
    );
  });

  it('surfaces structured server field errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          errors: [{ field: 'employer_bn', code: 'INVALID', hint: 'bad bn' }],
        }),
        { status: 422, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    render(<SubmitClaimPage />);
    fillValidForm();
    fireEvent.click(screen.getByRole('button', { name: /Submit claim/i }));

    await waitFor(() => {
      const bn = screen.getByLabelText(/Business Number/i);
      expect(bn).toHaveAttribute('aria-invalid', 'true');
    });
  });

  it('renders French labels when the language is French (Req 1.9)', async () => {
    await i18n.changeLanguage('fr');
    render(<SubmitClaimPage />);
    expect(
      screen.getByRole('heading', { name: /Présenter une demande/i }),
    ).toBeInTheDocument();
    await i18n.changeLanguage('en');
  });
});
