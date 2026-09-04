import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import en from './locales/en/translation.json';
import fr from './locales/fr/translation.json';

export const SUPPORTED_LANGUAGES = ['en', 'fr'] as const;
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number];

export const DEFAULT_LANGUAGE: SupportedLanguage = 'en';

const LANGUAGE_STORAGE_KEY = 'ei-ads.lang';

/**
 * Normalise an arbitrary language tag (e.g. "fr-CA", "EN") to a supported
 * language code, falling back to {@link DEFAULT_LANGUAGE} when unsupported.
 */
export function normaliseLanguage(
  value: string | null | undefined,
): SupportedLanguage {
  if (!value) {
    return DEFAULT_LANGUAGE;
  }
  const base = value.toLowerCase().split('-')[0];
  return (SUPPORTED_LANGUAGES as readonly string[]).includes(base)
    ? (base as SupportedLanguage)
    : DEFAULT_LANGUAGE;
}

/**
 * Resolve the initial UI language.
 *
 * Precedence (Req 20.1 — default to the language preference set in the
 * user's GCKey profile):
 *   1. Explicit preference persisted from a prior GCKey session.
 *   2. The browser's preferred language.
 *   3. {@link DEFAULT_LANGUAGE}.
 *
 * Once the authenticated session is loaded, {@link applyProfileLanguage}
 * overrides this with the GCKey profile preference unless the user has
 * manually chosen a language during the current session.
 */
function resolveInitialLanguage(): SupportedLanguage {
  if (typeof window !== 'undefined') {
    const stored = window.localStorage?.getItem(LANGUAGE_STORAGE_KEY);
    if (stored) {
      return normaliseLanguage(stored);
    }
    const browser = window.navigator?.language;
    if (browser) {
      return normaliseLanguage(browser);
    }
  }
  return DEFAULT_LANGUAGE;
}

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    fr: { translation: fr },
  },
  lng: resolveInitialLanguage(),
  fallbackLng: DEFAULT_LANGUAGE,
  supportedLngs: SUPPORTED_LANGUAGES as unknown as string[],
  interpolation: {
    escapeValue: false,
  },
  returnNull: false,
});

/** Keep the document `lang` attribute in sync for accessibility (WCAG 3.1.1). */
i18n.on('languageChanged', (lng: string) => {
  if (typeof document !== 'undefined') {
    document.documentElement.lang = normaliseLanguage(lng);
  }
});

/**
 * Apply the language preference from the authenticated GCKey profile.
 * This is the authoritative default per Req 20.1. It does not override an
 * explicit in-session choice already persisted by {@link setUserLanguage}.
 */
export async function applyProfileLanguage(
  profileLang: string | null | undefined,
): Promise<void> {
  const hasExplicitChoice =
    typeof window !== 'undefined' &&
    window.localStorage?.getItem(LANGUAGE_STORAGE_KEY) != null;
  if (hasExplicitChoice) {
    return;
  }
  const lang = normaliseLanguage(profileLang);
  if (i18n.language !== lang) {
    await i18n.changeLanguage(lang);
  }
}

/** Persist and apply a user-selected language (manual override). */
export async function setUserLanguage(lang: SupportedLanguage): Promise<void> {
  if (typeof window !== 'undefined') {
    window.localStorage?.setItem(LANGUAGE_STORAGE_KEY, lang);
  }
  await i18n.changeLanguage(lang);
}

export default i18n;
