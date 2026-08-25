import { useSyncExternalStore } from "react";
import { AtlasApp, type ProviderState } from "./atlas/AtlasApp";
import { httpAtlasApi, type AtlasApi } from "./api/atlasClient";
import { httpSliceApi, type SliceApi } from "./api/client";
import { ProposalWorkspace } from "./ProposalWorkspace";

const routeEvent = "drydock:navigate";

function subscribeRoute(listener: () => void) {
  window.addEventListener("popstate", listener);
  window.addEventListener(routeEvent, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(routeEvent, listener);
  };
}

const routeSnapshot = () => `${window.location.pathname}${window.location.search}`;

export function navigate(href: string, replace = false) {
  if (replace) window.history.replaceState(null, "", href);
  else window.history.pushState(null, "", href);
  window.dispatchEvent(new Event(routeEvent));
}

export function App({ api = httpSliceApi, atlasApi = httpAtlasApi, providerReadiness }: { api?: SliceApi; atlasApi?: AtlasApi; providerReadiness?: () => Promise<ProviderState> }) {
  const location = useSyncExternalStore(subscribeRoute, routeSnapshot, () => "/");
  const atlasActive = location.split("?", 1)[0].startsWith("/campaigns/");

  return (
    <>
      <ProposalWorkspace api={api} atlasApi={atlasApi} active={!atlasActive} navigate={navigate} location={location} />
      {atlasActive && <AtlasApp api={atlasApi} sliceApi={api} readiness={providerReadiness ?? api.readiness} location={location} navigate={navigate} />}
    </>
  );
}
