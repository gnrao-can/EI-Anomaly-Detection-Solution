import { useTranslation } from 'react-i18next';

import type { SupportedLanguage } from '../../i18n/config';
import {
  selectBilingual,
  type Indicator,
  type Severity,
} from '../../api/investigatorClaim';

/**
 * Renders the list of Indicators for a flagged claim with severity badges and
 * the LLM/engine explanation in the investigator's selected official language
 * (Req 22.2, 20.2).
 *
 * Severity is conveyed with both a coloured badge and its text label so the
 * meaning does not rely on colour alone (WCAG 1.4.1). Indicators are ordered by
 * severity, most severe first, so the highest-signal evidence surfaces at the
 * top of the case.
 */

export interface IndicatorListProps {
  indicators: readonly Indicator[];
}

const SEVERITY_ORDER: Record<Severity, number> = {
  CRITICAL: 0,
  HIGH: 1,
  MEDIUM: 2,
  LOW: 3,
  UNRESOLVED: 4,
};

export default function IndicatorList({ indicators }: IndicatorListProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.language as SupportedLanguage;

  const ordered = [...indicators].sort(
    (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
  );

  return (
    <section aria-labelledby="indicators-heading" className="panel">
      <h3 id="indicators-heading" className="section-title">
        {t('investigator.indicators.title')}
      </h3>

      {ordered.length === 0 ? (
        <p>{t('investigator.indicators.empty')}</p>
      ) : (
        <ul className="indicator-list">
          {ordered.map((indicator) => {
            const explanation = selectBilingual(
              indicator.explanationEn,
              indicator.explanationFr,
              language,
            );
            const severityLabel = t(
              `investigator.severity.${indicator.severity}`,
            );
            return (
              <li key={indicator.indicatorId} className="indicator-item">
                <span
                  className={`severity-badge severity-${indicator.severity.toLowerCase()}`}
                >
                  {/* Visible label plus a redundant textual cue for AT. */}
                  <span className="sr-only">
                    {t('investigator.indicators.severityPrefix')}{' '}
                  </span>
                  {severityLabel}
                </span>{' '}
                <span className="indicator-title">{indicator.title}</span>
                {explanation && (
                  <p className="indicator-explanation">{explanation}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
