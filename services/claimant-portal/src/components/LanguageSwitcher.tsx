import { useTranslation } from 'react-i18next';

import { normaliseLanguage, setUserLanguage } from '../i18n/config';

/**
 * Toggles between the two official languages. Selecting a language is a manual
 * override that persists for the session (see i18n/config setUserLanguage).
 */
export default function LanguageSwitcher() {
  const { t, i18n } = useTranslation();
  const current = normaliseLanguage(i18n.language);
  const target = current === 'en' ? 'fr' : 'en';

  return (
    <button
      type="button"
      lang={target}
      onClick={() => {
        void setUserLanguage(target);
      }}
    >
      {target === 'fr' ? t('language.switchToFrench') : t('language.switchToEnglish')}
    </button>
  );
}
