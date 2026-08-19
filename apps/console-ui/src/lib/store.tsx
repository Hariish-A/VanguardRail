/**
 * Session state: the connection, the verified identity, and what it may do.
 *
 * One deliberate rule runs through this file — **a key is not stored until the server
 * has accepted it.** `connect()` calls `/v1/me` first and only persists on success. The
 * alternative (save, then discover on the next page that every request 401s) produces a
 * console that looks connected and is not, which is the worst state for a tool whose job
 * is telling you whether something is being governed.
 *
 * `can()` reads the capability list the *server* returned. The console never derives
 * permissions from the role string, because the server has a break-glass grant that the
 * role does not describe: a key with the `agent` role named in
 * `GUARDRAIL_POLICY_ADMIN_KEY_IDS` really can publish policy.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  api,
  clearSession,
  loadSession,
  saveSession,
  type Session,
} from "./api";
import type { Capability, Identity } from "./types";

type Status = "disconnected" | "connecting" | "connected";

interface Store {
  session: Session;
  identity: Identity | null;
  status: Status;
  error: unknown;
  connect: (next: Session) => Promise<boolean>;
  disconnect: () => void;
  /** Whether the *server* said this key holds the capability. */
  can: (capability: Capability) => boolean;
}

const StoreContext = createContext<Store | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>(() => loadSession());
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [status, setStatus] = useState<Status>("disconnected");
  const [error, setError] = useState<unknown>(null);

  const connect = useCallback(async (next: Session): Promise<boolean> => {
    setStatus("connecting");
    setError(null);
    try {
      const who = await api.me(next);
      // Persisted only now, after the server accepted the credential.
      saveSession(next);
      setSession(next);
      setIdentity(who);
      setStatus("connected");
      return true;
    } catch (cause) {
      setError(cause);
      setIdentity(null);
      setStatus("disconnected");
      return false;
    }
  }, []);

  const disconnect = useCallback(() => {
    clearSession();
    setSession({ baseUrl: "", apiKey: "", agentUrl: "", agentKey: "" });
    setIdentity(null);
    setStatus("disconnected");
    setError(null);
  }, []);

  // Reconnect a reloaded tab. sessionStorage survives F5 but not closing the tab, so
  // this restores a working page without ever asking for the key twice in one sitting.
  useEffect(() => {
    const stored = loadSession();
    if (stored.baseUrl && stored.apiKey) void connect(stored);
    // Intentionally once, on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const can = useCallback(
    (capability: Capability) => identity?.capabilities.includes(capability) ?? false,
    [identity],
  );

  const value = useMemo<Store>(
    () => ({ session, identity, status, error, connect, disconnect, can }),
    [session, identity, status, error, connect, disconnect, can],
  );

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useSession(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useSession must be used inside <SessionProvider>");
  return store;
}

/**
 * Fetch-on-mount with explicit loading, error, and refresh.
 *
 * Deliberately tiny and local rather than a data library. Every screen here loads one or
 * two documents and offers a manual refresh; a cache layer would add a way for the
 * console to show a *stale* decision, which in this product is a correctness problem
 * rather than a performance one.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[],
): {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
  setData: (value: T | null) => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((value) => {
        if (!cancelled) {
          setData(value);
          setError(null);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause);
          setData(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return {
    data,
    error,
    loading,
    reload: () => setNonce((n) => n + 1),
    setData,
  };
}

/**
 * Hash-based routing.
 *
 * `#/audit` rather than `/audit`, because the console is served as a single S3 object.
 * Path routing needs a server that rewrites unknown paths to `index.html`; S3 static
 * hosting will not, and CloudFront — which would — cannot be created on this account.
 * A hash never reaches the server, so deep links and refreshes both work.
 */
export function useRoute(): [string, (next: string) => void] {
  const [route, setRoute] = useState(() => window.location.hash.slice(1) || "/");

  useEffect(() => {
    const onChange = () => setRoute(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = useCallback((next: string) => {
    window.location.hash = next;
  }, []);

  return [route, navigate];
}
