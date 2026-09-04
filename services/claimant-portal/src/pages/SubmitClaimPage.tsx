import { useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  submitClaim,
  type ServerFieldError,
} from '../api/claimSubmission';
import {
  validateClaimFields,
  type ClaimField,
  type ClaimFormValues,
  type FieldError,
} from '../validation/claim';

/** Visual field order — also the order the error summary and focus follow. */
const FIELD_ORDER: readonly ClaimField[] = [
  'sin',
  'employerBn',
  'insurableHours',
  'insurableEarnings',
  'separationDate',
  'separationReason',
] as const;

/** Map the server's snake_case field names onto our camelCase form fields. */
const SERVER_FIELD_MAP: Record<string, ClaimField> = {
  sin: 'sin',
  employer_bn: 'employerBn',
  insurable_hours: 'insurableHours',
  insurable_earnings: 'insurableEarnings',
  separation_date: 'separationDate',
  separation_reason: 'separationReason',
};

const EMPTY_FORM: ClaimFormValues = {
  sin: '',
  employerBn: '',
  insurableHours: '',
  insurableEarnings: '',
  separationDate: '',
  separationReason: '',
};

type FieldErrorMap = Partial<Record<ClaimField, string>>;

/**
 * The EI claim submission form.
 *
 * Accessibility (Req 1.10, WCAG 2.1 AA):
 *  - Every input has a programmatically associated <label> and, where present,
 *    hint/error text linked via `aria-describedby`.
 *  - Invalid fields set `aria-invalid` and their error text carries
 *    `role="alert"` so assistive tech announces it.
 *  - On a failed submit, an error summary (`role="alert"`) lists every problem
 *    and focus moves to it, then the first invalid control is focused.
 *  - Success renders a `role="status"` confirmation with the Claim_ID.
 *
 * Bilingual output (Req 1.9): all labels, hints, and error messages come from
 * i18next; error codes from validation map to `submitClaim.errors.<field>.<code>`.
 */
export default function SubmitClaimPage() {
  const { t } = useTranslation();

  const [values, setValues] = useState<ClaimFormValues>(EMPTY_FORM);
  const [roeFile, setRoeFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<FieldErrorMap>({});
  const [submitting, setSubmitting] = useState(false);
  const [claimId, setClaimId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const summaryRef = useRef<HTMLDivElement>(null);

  const errorEntries = useMemo(
    () => FIELD_ORDER.filter((f) => errors[f]).map((f) => ({ field: f, message: errors[f]! })),
    [errors],
  );

  function updateField(field: ClaimField, value: string) {
    setValues((prev) => ({ ...prev, [field]: value }));
    // Clear a field's error as the claimant corrects it.
    setErrors((prev) => {
      if (!prev[field]) {
        return prev;
      }
      const next = { ...prev };
      delete next[field];
      return next;
    });
  }

  /** Translate a client-side validation error code into a localised message. */
  function messageForCode(err: FieldError): string {
    return t(`submitClaim.errors.${err.field}.${err.code}`);
  }

  /** Translate a server field error, preferring a code-based localised message. */
  function messageForServerError(err: ServerFieldError): string | null {
    const field = SERVER_FIELD_MAP[err.field];
    if (!field) {
      return null;
    }
    const key = `submitClaim.errors.${field}.${err.code}`;
    const localised = t(key);
    // If no translation exists, fall back to the server-provided hint.
    if (localised !== key) {
      return localised;
    }
    return err.hint ?? t('submitClaim.errors.generic');
  }

  function focusFirstError(nextErrors: FieldErrorMap) {
    // Move focus to the summary first so the screen reader announces the count,
    // leaving in-field navigation to the claimant.
    if (summaryRef.current) {
      summaryRef.current.focus();
      return;
    }
    const first = FIELD_ORDER.find((f) => nextErrors[f]);
    if (first) {
      document.getElementById(first)?.focus();
    }
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    setClaimId(null);

    // 1. Client-side validation (fast, accessible feedback; mirrors server).
    const clientErrors = validateClaimFields(values);
    if (clientErrors.length > 0) {
      const map: FieldErrorMap = {};
      for (const err of clientErrors) {
        map[err.field] = messageForCode(err);
      }
      setErrors(map);
      // Defer focus until the summary has rendered.
      window.setTimeout(() => focusFirstError(map), 0);
      return;
    }

    setErrors({});
    setSubmitting(true);

    // 2. Submit to the Claim Intake Service.
    const result = await submitClaim(values, roeFile);
    setSubmitting(false);

    if (result.ok) {
      setClaimId(result.claimId);
      setValues(EMPTY_FORM);
      setRoeFile(null);
      return;
    }

    // 3. Render structured server errors, or a generic transport error.
    if (result.fieldErrors.length > 0) {
      const map: FieldErrorMap = {};
      for (const err of result.fieldErrors) {
        const field = SERVER_FIELD_MAP[err.field];
        const message = messageForServerError(err);
        if (field && message) {
          map[field] = message;
        }
      }
      if (Object.keys(map).length > 0) {
        setErrors(map);
        window.setTimeout(() => focusFirstError(map), 0);
        return;
      }
    }

    setFormError(
      result.network ? t('submitClaim.errors.network') : t('submitClaim.errors.server'),
    );
    window.setTimeout(() => summaryRef.current?.focus(), 0);
  }

  // --- Confirmation view -------------------------------------------------
  if (claimId) {
    return (
      <section aria-labelledby="submit-heading">
        <h1 id="submit-heading">{t('submitClaim.heading')}</h1>
        <div role="status" aria-live="polite">
          <h2>{t('submitClaim.confirmation.heading')}</h2>
          <p>{t('submitClaim.confirmation.body')}</p>
          <p>
            {t('submitClaim.confirmation.claimIdLabel')}{' '}
            <strong>{claimId}</strong>
          </p>
        </div>
      </section>
    );
  }

  const hasSummary = errorEntries.length > 0 || formError !== null;

  return (
    <section aria-labelledby="submit-heading">
      <h1 id="submit-heading">{t('submitClaim.heading')}</h1>
      <p>{t('submitClaim.intro')}</p>

      {hasSummary && (
        <div
          ref={summaryRef}
          role="alert"
          tabIndex={-1}
          aria-labelledby="error-summary-heading"
        >
          <h2 id="error-summary-heading">{t('submitClaim.errorSummary.heading')}</h2>
          {formError && <p>{formError}</p>}
          {errorEntries.length > 0 && (
            <ul>
              {errorEntries.map((entry) => (
                <li key={entry.field}>
                  <a href={`#${entry.field}`}>
                    {t(`submitClaim.fields.${entry.field}.label`)}: {entry.message}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <TextField
          field="sin"
          value={values.sin}
          error={errors.sin}
          onChange={updateField}
          inputMode="numeric"
          autoComplete="off"
        />
        <TextField
          field="employerBn"
          value={values.employerBn}
          error={errors.employerBn}
          onChange={updateField}
          autoComplete="off"
        />
        <TextField
          field="insurableHours"
          value={values.insurableHours}
          error={errors.insurableHours}
          onChange={updateField}
          type="number"
          inputMode="decimal"
        />
        <TextField
          field="insurableEarnings"
          value={values.insurableEarnings}
          error={errors.insurableEarnings}
          onChange={updateField}
          type="number"
          inputMode="decimal"
        />
        <TextField
          field="separationDate"
          value={values.separationDate}
          error={errors.separationDate}
          onChange={updateField}
          type="date"
        />

        <div>
          <label htmlFor="separationReason">
            {t('submitClaim.fields.separationReason.label')}{' '}
            <span aria-hidden="true">*</span>
          </label>
          <p id="separationReason-hint">
            {t('submitClaim.fields.separationReason.hint')}
          </p>
          <textarea
            id="separationReason"
            name="separationReason"
            value={values.separationReason}
            required
            aria-required="true"
            aria-invalid={errors.separationReason ? true : undefined}
            aria-describedby={describedBy('separationReason', errors.separationReason)}
            onChange={(e) => updateField('separationReason', e.target.value)}
          />
          {errors.separationReason && (
            <p id="separationReason-error" role="alert">
              {errors.separationReason}
            </p>
          )}
        </div>

        <div>
          <label htmlFor="roeDocument">
            {t('submitClaim.fields.roeDocument.label')}
          </label>
          <p id="roeDocument-hint">{t('submitClaim.fields.roeDocument.hint')}</p>
          <input
            id="roeDocument"
            name="roeDocument"
            type="file"
            accept=".pdf,.xml,.json,application/pdf,application/xml,text/xml,application/json"
            aria-describedby="roeDocument-hint"
            onChange={(e) => setRoeFile(e.target.files?.[0] ?? null)}
          />
        </div>

        <button type="submit" disabled={submitting}>
          {submitting
            ? t('submitClaim.submitting')
            : t('submitClaim.submit')}
        </button>
      </form>
    </section>
  );
}

/** Compute the `aria-describedby` value linking a field's hint and error. */
function describedBy(field: string, error: string | undefined): string {
  const ids = [`${field}-hint`];
  if (error) {
    ids.push(`${field}-error`);
  }
  return ids.join(' ');
}

interface TextFieldProps {
  field: ClaimField;
  value: string;
  error: string | undefined;
  onChange: (field: ClaimField, value: string) => void;
  type?: string;
  inputMode?: 'numeric' | 'decimal' | 'text';
  autoComplete?: string;
}

/** A labelled, described, WCAG-compliant single-line input. */
function TextField({
  field,
  value,
  error,
  onChange,
  type = 'text',
  inputMode,
  autoComplete,
}: TextFieldProps) {
  const { t } = useTranslation();
  return (
    <div>
      <label htmlFor={field}>
        {t(`submitClaim.fields.${field}.label`)} <span aria-hidden="true">*</span>
      </label>
      <p id={`${field}-hint`}>{t(`submitClaim.fields.${field}.hint`)}</p>
      <input
        id={field}
        name={field}
        type={type}
        inputMode={inputMode}
        autoComplete={autoComplete}
        value={value}
        required
        aria-required="true"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(field, error)}
        onChange={(e) => onChange(field, e.target.value)}
      />
      {error && (
        <p id={`${field}-error`} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
