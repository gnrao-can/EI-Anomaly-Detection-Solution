import { useTranslation } from 'react-i18next';

export default function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="notfound-heading">
      <h1 id="notfound-heading">{t('errors.notFound')}</h1>
    </section>
  );
}
