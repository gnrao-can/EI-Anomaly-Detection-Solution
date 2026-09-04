import { createBrowserRouter } from 'react-router-dom';

import Layout from './components/Layout';
import { RequireAuth } from './auth/RequireAuth';
import { ROLES } from './auth/roles';
import HomePage from './pages/HomePage';
import SubmitClaimPage from './pages/SubmitClaimPage';
import MyClaimsPage from './pages/MyClaimsPage';
import ClaimDetailPage from './pages/ClaimDetailPage';
import DashboardPage from './pages/DashboardPage';
import InvestigatorClaimDetailPage from './pages/InvestigatorClaimDetailPage';
import AuthCallbackPage from './pages/AuthCallbackPage';
import NotFoundPage from './pages/NotFoundPage';

/**
 * Application routes with RBAC-based guards (Req 22.6).
 *
 *  /                — public
 *  /auth/callback   — GCKey OAuth 2.0 redirect target (PKCE completion)
 *  /submit, /claims       — CLAIMANT only
 *  /dashboard             — REVIEWER, INVESTIGATOR, ADMINISTRATOR
 *  /dashboard/claims/:id  — INVESTIGATOR, ADMINISTRATOR (Req 22.6)
 */
export const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <HomePage /> },
      { path: 'auth/callback', element: <AuthCallbackPage /> },
      {
        path: 'submit',
        element: (
          <RequireAuth roles={[ROLES.CLAIMANT]}>
            <SubmitClaimPage />
          </RequireAuth>
        ),
      },
      {
        path: 'claims',
        element: (
          <RequireAuth roles={[ROLES.CLAIMANT]}>
            <MyClaimsPage />
          </RequireAuth>
        ),
      },
      {
        path: 'claims/:claimId',
        element: (
          <RequireAuth roles={[ROLES.CLAIMANT]}>
            <ClaimDetailPage />
          </RequireAuth>
        ),
      },
      {
        path: 'dashboard',
        element: (
          <RequireAuth
            roles={[ROLES.REVIEWER, ROLES.INVESTIGATOR, ROLES.ADMINISTRATOR]}
          >
            <DashboardPage />
          </RequireAuth>
        ),
      },
      {
        // Investigator per-claim detail (Req 22.2–22.5). Only INVESTIGATOR and
        // ADMINISTRATOR may open flagged-claim details (Req 22.6); REVIEWER is
        // intentionally excluded from the detail view.
        path: 'dashboard/claims/:claimId',
        element: (
          <RequireAuth roles={[ROLES.INVESTIGATOR, ROLES.ADMINISTRATOR]}>
            <InvestigatorClaimDetailPage />
          </RequireAuth>
        ),
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]);
