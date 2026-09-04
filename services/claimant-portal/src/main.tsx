import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import './i18n/config';
import './styles/accessibility.css';
import './styles/theme.css';
import './styles/app.css';
import App from './App';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found.');
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
