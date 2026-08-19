/// <reference types="vite/client" />

/**
 * Build-time configuration.
 *
 * Only URLs and a build identifier. **No credential is ever baked in** — the deployed
 * bundle is a public artifact, so a key embedded here would be published.
 */
interface ImportMetaEnv {
  readonly VITE_GUARDRAIL_BASE_URL?: string;
  readonly VITE_GUARDRAIL_AGENT_URL?: string;
  readonly VITE_GUARDRAIL_VERSION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
