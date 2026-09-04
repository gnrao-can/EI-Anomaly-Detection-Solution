/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** GCKey OAuth 2.0 authorization endpoint (authorize URL). */
  readonly VITE_GCKEY_AUTHORIZE_URL: string;
  /** OAuth client_id registered for the Claimant Portal. */
  readonly VITE_GCKEY_CLIENT_ID: string;
  /** Redirect/callback URI registered with GCKey. */
  readonly VITE_GCKEY_REDIRECT_URI: string;
  /** Space-separated OAuth scopes requested from GCKey. */
  readonly VITE_GCKEY_SCOPES: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
