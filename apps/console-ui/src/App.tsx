/**
 * Routing and the connection gate.
 *
 * The Overview is readable without a key — it is the page a judge lands on, and gating it
 * behind a credential would hide the explanation behind the thing it explains. Every page
 * that reads tenant data requires a connection and says so, rather than rendering an
 * empty table that looks like "no activity".
 */

import { Aurora } from "@/components/effects";
import { Shell } from "@/components/Shell";
import { AgentConsolePage } from "@/pages/AgentConsole";
import { AuditChainPage } from "@/pages/AuditChain";
import { ConnectPage } from "@/pages/Connect";
import { DecisionTheatrePage } from "@/pages/DecisionTheatre";
import { OverviewPage } from "@/pages/Overview";
import { ReviewQueuePage } from "@/pages/ReviewQueue";
import { SystemHealthPage } from "@/pages/SystemHealth";
import { SessionProvider, useRoute, useSession } from "@/lib/store";

function Routed() {
  const [route] = useRoute();
  const { status, session } = useSession();

  // First visit with nothing stored: show the connection screen full-bleed rather than
  // dropping the reader into a console whose every panel says "not connected".
  const firstRun =
    status === "disconnected" && !session.apiKey && route !== "/" && route !== "/connect";

  if (firstRun) return <ConnectPage standalone />;

  const page = (() => {
    switch (route) {
      case "/connect":
        return <ConnectPage />;
      case "/agent":
        return <AgentConsolePage />;
      case "/theatre":
        return <DecisionTheatrePage />;
      case "/review":
        return <ReviewQueuePage />;
      case "/audit":
        return <AuditChainPage />;
      case "/health":
        return <SystemHealthPage />;
      case "/":
        return <OverviewPage />;
      default:
        return <OverviewPage />;
    }
  })();

  return (
    <>
      <Aurora />
      <Shell route={route}>{page}</Shell>
    </>
  );
}

export default function App() {
  return (
    <SessionProvider>
      <Routed />
    </SessionProvider>
  );
}
