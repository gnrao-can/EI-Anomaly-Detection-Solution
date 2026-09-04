/**
 * Client-side claim validation.
 *
 * These rules MIRROR the authoritative server-side rules in
 * `shared/validation.py` (SIN Luhn, BN format, insurable hours/earnings > 0,
 * separation date <= today). Client-side validation exists only to give the
 * claimant fast, accessible, inline feedback (Req 1.3, 1.9); the backend
 * remains the source of truth and independently re-validates every field.
 *
 * Each rule returns a machine-readable error **code** (never a human string),
 * so the UI layer can render the message in the claimant's chosen official
 * language via i18next (Req 1.9). Field + code map to a translation key of the
 * form `submitClaim.errors.<field>.<code>`.
 *
 * Requirements: 1.9, 1.10
 */

/** Fields that make up an EI claim submission form. */
export type ClaimField =
  | 'sin'
  | 'employerBn'
  | 'insurableHours'
  | 'insurableEarnings'
  | 'separationDate'
  | 'separationReason';

/** Machine-readable validation error codes (shared with the backend contract). */
export type ClaimErrorCode = 'MISSING' | 'INVALID' | 'TOO_LARGE';

/** A single field validation error. */
export interface FieldError {
  field: ClaimField;
  code: ClaimErrorCode;
}

/** Raw, unparsed form values as entered by the claimant (all strings). */
export interface ClaimFormValues {
  sin: string;
  employerBn: string;
  insurableHours: string;
  insurableEarnings: string;
  separationDate: string;
  separationReason: string;
}

/** Maximum insurable hours: 60 hours/week x 52 weeks (mirror of validation.py). */
export const MAX_INSURABLE_HOURS = 3120;

/** Maximum length of the free-text separation reason (mirror of Claim model). */
export const MAX_SEPARATION_REASON_LENGTH = 1000;

/** Valid CRA BN program identifier codes (mirror of validation.py). */
const VALID_BN_PROGRAM_CODES: ReadonlySet<string> = new Set([
  'RT',
  'RP',
  'RC',
  'RZ',
  'RU',
  'RM',
]);

/**
 * Apply the Luhn algorithm to a digits-only string.
 * Returns true when the number passes the Luhn check.
 */
export function luhnChecksum(digits: string): boolean {
  if (!/^\d+$/.test(digits)) {
    return false;
  }
  let total = 0;
  const reversed = digits.split('').reverse();
  for (let i = 0; i < reversed.length; i += 1) {
    let n = Number(reversed[i]);
    if (i % 2 === 1) {
      n *= 2;
      if (n > 9) {
        n -= 9;
      }
    }
    total += n;
  }
  return total % 10 === 0;
}

/** Strip spaces and dashes from a SIN. */
export function normaliseSin(sin: string): string {
  return sin.replace(/[\s-]/g, '');
}

/**
 * Validate a Canadian Social Insurance Number: exactly 9 digits, does not
 * start with 0, and passes the Luhn checksum (mirror of validation.py).
 */
export function validateSin(sin: string): boolean {
  const cleaned = normaliseSin(sin);
  if (cleaned.length !== 9) {
    return false;
  }
  if (!/^\d{9}$/.test(cleaned)) {
    return false;
  }
  if (cleaned[0] === '0') {
    return false;
  }
  return luhnChecksum(cleaned);
}

/**
 * Validate a CRA Business Number: either a 9-digit base BN, or a 15-character
 * BN (9 digits + 2-letter program code + 4-digit account) (mirror of
 * validation.py). Case-insensitive on the program code.
 */
export function validateBn(bn: string): boolean {
  const stripped = bn.trim();
  if (stripped.length === 9) {
    return /^\d{9}$/.test(stripped);
  }
  if (stripped.length === 15) {
    const base = stripped.slice(0, 9);
    const program = stripped.slice(9, 11).toUpperCase();
    const account = stripped.slice(11);
    return (
      /^\d{9}$/.test(base) &&
      VALID_BN_PROGRAM_CODES.has(program) &&
      /^\d{4}$/.test(account)
    );
  }
  return false;
}

/**
 * Parse a date input value (YYYY-MM-DD) into a UTC-midnight Date, or null if
 * the string is not a well-formed ISO calendar date. Mirrors the strictness of
 * Python's `date.fromisoformat` by round-tripping the parsed components.
 */
export function parseIsoDate(value: string): Date | null {
  const trimmed = value.trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  // Reject overflow (e.g. 2024-02-31 -> March) by verifying the round-trip.
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null;
  }
  return date;
}

/** Today at UTC midnight, for a date-only "not in the future" comparison. */
function todayUtcMidnight(now: Date): Date {
  return new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()),
  );
}

/**
 * Validate all mandatory claim fields and return a list of field errors.
 * An empty list means the form is valid. Ordering follows the visual field
 * order so focus management can target the first error deterministically.
 *
 * @param values the raw form values.
 * @param now injectable clock (defaults to the current time) for testability.
 */
export function validateClaimFields(
  values: ClaimFormValues,
  now: Date = new Date(),
): FieldError[] {
  const errors: FieldError[] = [];

  // --- SIN ---
  if (values.sin.trim() === '') {
    errors.push({ field: 'sin', code: 'MISSING' });
  } else if (!validateSin(values.sin)) {
    errors.push({ field: 'sin', code: 'INVALID' });
  }

  // --- Employer BN ---
  if (values.employerBn.trim() === '') {
    errors.push({ field: 'employerBn', code: 'MISSING' });
  } else if (!validateBn(values.employerBn)) {
    errors.push({ field: 'employerBn', code: 'INVALID' });
  }

  // --- Insurable hours ---
  if (values.insurableHours.trim() === '') {
    errors.push({ field: 'insurableHours', code: 'MISSING' });
  } else {
    const hours = Number(values.insurableHours);
    if (!Number.isFinite(hours)) {
      errors.push({ field: 'insurableHours', code: 'INVALID' });
    } else if (hours <= 0) {
      errors.push({ field: 'insurableHours', code: 'INVALID' });
    } else if (hours > MAX_INSURABLE_HOURS) {
      errors.push({ field: 'insurableHours', code: 'TOO_LARGE' });
    }
  }

  // --- Insurable earnings ---
  if (values.insurableEarnings.trim() === '') {
    errors.push({ field: 'insurableEarnings', code: 'MISSING' });
  } else {
    const earnings = Number(values.insurableEarnings);
    if (!Number.isFinite(earnings) || earnings <= 0) {
      errors.push({ field: 'insurableEarnings', code: 'INVALID' });
    }
  }

  // --- Separation date ---
  if (values.separationDate.trim() === '') {
    errors.push({ field: 'separationDate', code: 'MISSING' });
  } else {
    const parsed = parseIsoDate(values.separationDate);
    if (parsed === null) {
      errors.push({ field: 'separationDate', code: 'INVALID' });
    } else if (parsed.getTime() > todayUtcMidnight(now).getTime()) {
      errors.push({ field: 'separationDate', code: 'INVALID' });
    }
  }

  // --- Separation reason ---
  if (values.separationReason.trim() === '') {
    errors.push({ field: 'separationReason', code: 'MISSING' });
  } else if (values.separationReason.length > MAX_SEPARATION_REASON_LENGTH) {
    errors.push({ field: 'separationReason', code: 'TOO_LARGE' });
  }

  return errors;
}
