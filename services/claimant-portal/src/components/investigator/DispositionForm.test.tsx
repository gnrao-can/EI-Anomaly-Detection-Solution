import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import '../../i18n/config';
import DispositionForm from './DispositionForm';

afterEach(() => vi.restoreAllMocks());

describe('DispositionForm', () => {
  it('requires a disposition selection before submit', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    render(<DispositionForm claimId="c-1" />);

    fireEvent.click(screen.getByRole('button', { name: /Save disposition/i }));

    expect(
      await screen.findByText(/Select a disposition before submitting/i),
    ).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('submits the selected disposition and case notes, then announces success', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({}) } as Response);
    const onSubmitted = vi.fn();

    render(<DispositionForm claimId="c-42" onSubmitted={onSubmitted} />);

    fireEvent.click(screen.getByLabelText(/Confirmed fraud/i));
    fireEvent.change(screen.getByLabelText(/Case notes/i), {
      target: { value: 'Verified with CRA records.' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Save disposition/i }));

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/claims/c-42/disposition');
    expect(init?.method).toBe('PUT');
    const body = JSON.parse((init?.body as string) ?? '{}');
    expect(body.disposition).toBe('CONFIRMED_FRAUD');
    expect(body.case_notes).toBe('Verified with CRA records.');

    expect(await screen.findByText(/Disposition saved/i)).toBeInTheDocument();
    expect(onSubmitted).toHaveBeenCalledWith('CONFIRMED_FRAUD');
  });

  it('shows an error alert when the submission fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Server Error',
      clone: () => ({ json: async () => ({}) }),
      json: async () => ({}),
    } as unknown as Response);

    render(<DispositionForm claimId="c-1" />);
    fireEvent.click(screen.getByLabelText(/False positive/i));
    fireEvent.click(screen.getByRole('button', { name: /Save disposition/i }));

    expect(
      await screen.findByText(/The disposition could not be saved/i),
    ).toBeInTheDocument();
  });

  it('pre-selects an existing disposition and notes', () => {
    render(
      <DispositionForm
        claimId="c-1"
        initialDisposition="NEEDS_FURTHER_REVIEW"
        initialCaseNotes="Awaiting employer response."
      />,
    );
    expect(
      (screen.getByLabelText(/Needs further review/i) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(
      (screen.getByLabelText(/Case notes/i) as HTMLTextAreaElement).value,
    ).toBe('Awaiting employer response.');
  });
});
