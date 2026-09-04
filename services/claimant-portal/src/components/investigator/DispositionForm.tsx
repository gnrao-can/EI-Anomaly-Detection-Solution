import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import {
  DISPOSITIONS,
  InvestigatorApiError,
  submitDisposition,
  type Disposition,
} from '../../api/investigatorClaim';

/**
 * Case-management form for recording an investigator disposition on a flagged
 * claim (Req 22.3, 22.4).
 *
 * The investigator chooses one of CONFIRMED_FRAUD, FALSE_POSITIVE, or
 * NEEDS_FURTHER_REVIEW and attaches free-text case notes, then submits. The
 * form PUTs to `/api/v1/claims/{claim_id}/disposition`; the backend publishes
 * the outcome to Kafka `outcome.feedback` when the disposition is
 * CONFIRMED_FRAUD or FALSE_POSITIVE (Req 22.4).
 *
 * Accessibility: the choices are a labelled radio group (`fieldset`/`legend`),
 * the notes textarea is associated with its label, validation errors are
 * announced via `role="alert"`, and the success state uses an `aria-live`
 * region so screen-reader users hear the outcome.
 */

export interface DispositionFormProps {
  claimId: string;
  /** Disposition already recorded, used to pre-select the radio group. */
  initialDisposition?: Disposition | null;
  /** Case notes already recorded, used to pre-fill the textarea. */
  initialCaseNotes?: string | null;
  /** Called after a successful submission (e.g. to refresh the detail). */
  onSubmitted?: (disposition: Disposition) => void;
}

type SubmitState =
  | { phase: 'idle' }
  | { phase: 'submitting' }
  | { phase: 'success'; disposition: Disposition }
  | { phase: 'error'; message: string };

export default function DispositionForm({
  claimId,
  initialDisposition = null,
  initialCaseNotes = null,
  onSubmitted,
}: DispositionFormProps) {
  const { t } = useTranslation();
  const [disposition, setDisposition] = useState<Disposition | null>(
    initialDisposition,
  );
  const [caseNotes, setCaseNotes] = useState<string>(initialCaseNotes ?? '');
  const [state, setState] = useState<SubmitState>({ phase: 'idle' });
  const [validationError, setValidationError] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!disposition) {
      setValidationError(t('investigator.disposition.selectRequired'));
      return;
    }
    setValidationError(null);
    setState({ phase: 'submitting' });
    try {
      await submitDisposition(claimId, { disposition, caseNotes });
      setState({ phase: 'success', disposition });
      onSubmitted?.(disposition);
    } catch (err) {
      const message =
        err instanceof InvestigatorApiError || err instanceof Error
          ? err.message
          : String(err);
      setState({ phase: 'error', message });
    }
  };

  const submitting = state.phase === 'submitting';

  return (
    <section aria-labelledby="disposition-heading" className="panel">
      <h3 id="disposition-heading" className="section-title">
        {t('investigator.disposition.title')}
      </h3>

      <form onSubmit={handleSubmit} noValidate>
        <fieldset>
          <legend>{t('investigator.disposition.legend')}</legend>
          {validationError && (
            <p role="alert" className="field-error">
              {validationError}
            </p>
          )}
          {DISPOSITIONS.map((option) => (
            <div key={option} className="radio-row">
              <input
                type="radio"
                id={`disposition-${option}`}
                name="disposition"
                value={option}
                checked={disposition === option}
                onChange={() => setDisposition(option)}
                disabled={submitting}
              />
              <label htmlFor={`disposition-${option}`}>
                {t(`investigator.disposition.option.${option}`)}
              </label>
            </div>
          ))}
        </fieldset>

        <div className="form-row">
          <label htmlFor="case-notes">
            {t('investigator.disposition.notesLabel')}
          </label>
          <textarea
            id="case-notes"
            name="caseNotes"
            rows={5}
            value={caseNotes}
            onChange={(event) => setCaseNotes(event.target.value)}
            disabled={submitting}
          />
        </div>

        <button type="submit" disabled={submitting}>
          {submitting
            ? t('investigator.disposition.submitting')
            : t('investigator.disposition.submit')}
        </button>
      </form>

      {/* Accessible outcome region. */}
      <p role="status" aria-live="polite">
        {state.phase === 'success' &&
          t('investigator.disposition.success', {
            disposition: t(
              `investigator.disposition.option.${state.disposition}`,
            ),
          })}
      </p>

      {state.phase === 'error' && (
        <p role="alert">
          {t('investigator.disposition.error', { message: state.message })}
        </p>
      )}
    </section>
  );
}
