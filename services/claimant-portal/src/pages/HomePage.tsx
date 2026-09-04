import { useTranslation } from 'react-i18next';

export default function HomePage() {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="home-heading" className="hero">
      <h1 id="home-heading">{t('home.heading')}</h1>
      <p>{t('home.intro')}</p>
    </section>
  );
}
