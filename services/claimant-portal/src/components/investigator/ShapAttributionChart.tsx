import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from 'recharts';

import type { SupportedLanguage } from '../../i18n/config';
import {
  selectBilingual,
  type Indicator,
  type ShapAttribution,
} from '../../api/investigatorClaim';

/**
 * Accessible SHAP attribution bar chart for the investigator detail view
 * (Req 22.2, 22.5).
 *
 * Each bar represents one Indicator's signed SHAP contribution to the
 * Risk_Score. Positive contributions push the score up (toward fraud);
 * negative contributions pull it down. Accessibility (WCAG 2.1 AA):
 *
 *   - The chart is an `img`-role group with a bilingual descriptive label.
 *   - Every bar carries an individual `aria-label` naming the Indicator and its
 *     contribution, so a screen reader can announce each data point.
 *   - A visually-hidden data table mirrors the chart exactly, giving assistive
 *     technology a fully structured, navigable alternative to the SVG.
 *   - The chart title and both axis labels are provided in EN and FR via
 *     i18next and follow the active UI language.
 *
 * Recharts (pinned 2.15.0) renders the visual bars; it does not expose the
 * per-bar text alternatives itself, hence the hidden table is authoritative
 * for screen-reader users.
 */

export interface ShapAttributionChartProps {
  attributions: readonly ShapAttribution[];
  indicators: readonly Indicator[];
}

interface ChartDatum {
  indicatorId: string;
  /** Short, human-readable label for the axis / table row. */
  label: string;
  contribution: number;
  /** Rounded value used in labels and the table for stable display. */
  display: number;
}

const POSITIVE_FILL = '#b91c1c'; // red-700 — increases risk
const NEGATIVE_FILL = '#15803d'; // green-700 — decreases risk

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

export default function ShapAttributionChart({
  attributions,
  indicators,
}: ShapAttributionChartProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.language as SupportedLanguage;

  // Build a lookup from indicatorId to its human-readable title so both the
  // chart axis and the hidden table label bars by Indicator rather than by an
  // opaque feature name.
  const data = useMemo<ChartDatum[]>(() => {
    const titleById = new Map<string, Indicator>();
    for (const ind of indicators) {
      titleById.set(ind.indicatorId, ind);
    }
    return attributions.map((attr) => {
      const indicator = titleById.get(attr.indicatorId);
      const label =
        (indicator && (indicator.title || indicator.engine)) ||
        attr.featureName ||
        attr.indicatorId;
      return {
        indicatorId: attr.indicatorId,
        label,
        contribution: attr.contribution,
        display: round3(attr.contribution),
      };
    });
  }, [attributions, indicators]);

  const title = t('investigator.shap.title');
  const featureAxis = t('investigator.shap.featureAxis');
  const contributionAxis = t('investigator.shap.contributionAxis');

  // Bilingual description used as the chart's accessible name (Req 22.5).
  const chartDescription = t('investigator.shap.description', {
    count: data.length,
  });

  if (data.length === 0) {
    return (
      <section aria-labelledby="shap-heading" className="panel">
        <h3 id="shap-heading" className="section-title">{title}</h3>
        <p>{t('investigator.shap.empty')}</p>
      </section>
    );
  }

  const formatContribution = (value: number): string =>
    new Intl.NumberFormat(language, {
      signDisplay: 'exceptZero',
      maximumFractionDigits: 3,
    }).format(value);

  return (
    <section aria-labelledby="shap-heading" className="panel">
      <h3 id="shap-heading" className="section-title">{title}</h3>

      {/*
        The rendered SVG chart. It is marked as an image with a descriptive
        label; the authoritative accessible data lives in the table below.
      */}
      <div role="img" aria-label={`${title}. ${chartDescription}`}>
        <ResponsiveContainer width="100%" height={Math.max(160, data.length * 44)}>
          <BarChart
            layout="vertical"
            data={data}
            margin={{ top: 8, right: 48, bottom: 24, left: 8 }}
          >
            <XAxis
              type="number"
              label={{ value: contributionAxis, position: 'bottom' }}
            />
            <YAxis
              type="category"
              dataKey="label"
              width={160}
              label={{
                value: featureAxis,
                angle: -90,
                position: 'insideLeft',
              }}
            />
            <Bar dataKey="contribution" isAnimationActive={false}>
              {data.map((datum) => (
                <Cell
                  key={datum.indicatorId}
                  fill={datum.contribution >= 0 ? POSITIVE_FILL : NEGATIVE_FILL}
                  aria-label={t('investigator.shap.barLabel', {
                    feature: datum.label,
                    value: formatContribution(datum.display),
                  })}
                />
              ))}
              <LabelList
                dataKey="display"
                position="right"
                formatter={(value: number) => formatContribution(value)}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/*
        Per-bar text alternatives (Req 22.5). Recharts does not reliably
        forward an aria-label onto each rendered SVG bar, so we emit one
        labelled, visually-hidden element per bar. Each carries an individual
        aria-label naming the Indicator and its signed contribution, giving a
        screen reader a discrete announceable node for every bar in the chart.
      */}
      <ul className="sr-only" aria-label={title}>
        {data.map((datum) => (
          <li
            key={`bar-${datum.indicatorId}`}
            aria-label={t('investigator.shap.barLabel', {
              feature: datum.label,
              value: formatContribution(datum.display),
            })}
          />
        ))}
      </ul>

      {/*
        Visually-hidden data table — the screen-reader alternative to the SVG
        (Req 22.5). `.sr-only` keeps it out of the visual layout while leaving
        it in the accessibility tree.
      */}
      <table className="sr-only">
        <caption>{chartDescription}</caption>
        <thead>
          <tr>
            <th scope="col">{featureAxis}</th>
            <th scope="col">{contributionAxis}</th>
            <th scope="col">{t('investigator.shap.directionAxis')}</th>
          </tr>
        </thead>
        <tbody>
          {data.map((datum) => (
            <tr key={datum.indicatorId}>
              <th scope="row">{datum.label}</th>
              <td>{formatContribution(datum.display)}</td>
              <td>
                {datum.contribution >= 0
                  ? t('investigator.shap.increasesRisk')
                  : t('investigator.shap.decreasesRisk')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export { selectBilingual };
