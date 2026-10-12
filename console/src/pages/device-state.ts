export function debugConsoleUrl(deviceId: string, baseUrl = "http://127.0.0.1:8792"): string | null {
  try {
    const url = new URL(baseUrl);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    url.searchParams.set("deviceId", deviceId);
    return url.href;
  } catch {
    return null;
  }
}

export function canDeviceAction(status: string, action: string, homeOk = true): boolean {
  switch (action) {
    case "pause":
      return ["ready", "busy", "returning_home", "offline", "degraded"].includes(status);
    case "resume":
      return status === "paused" && homeOk;
    case "quarantine":
      return ["starting", "ready", "busy", "returning_home", "offline", "degraded", "paused", "recovering"].includes(status);
    case "unquarantine":
      return status === "quarantined";
    default:
      return false;
  }
}

export function createLatestRequest() {
  let generation = 0;
  return {
    begin() {
      const current = ++generation;
      return () => current === generation;
    },
    invalidate() {
      generation += 1;
    },
  };
}

export interface BindingDraft {
  fields: Record<string, string>;
  resources: Record<string, string>;
  originalFields: Record<string, string>;
  originalResources: Record<string, string>;
}

export function newBindingDraft(fields: Record<string, string>, resources: Record<string, string>): BindingDraft {
  return { fields: { ...fields }, resources: { ...resources }, originalFields: fields, originalResources: resources };
}

export function bindingDraftDirty(draft: BindingDraft): boolean {
  return Object.keys(draft.fields).some((key) => draft.fields[key] !== draft.originalFields[key]) ||
    Object.keys(draft.resources).some((key) => Boolean(draft.resources[key]) &&
      draft.resources[key] !== draft.originalResources[key]);
}

export function refreshBindingEditor<T extends { draft: BindingDraft }>(
  current: T | null, incoming: T, reset = false,
): T {
  return !reset && current && bindingDraftDirty(current.draft) ? current : incoming;
}

// Pending creation owns the slot too; a late response must never adopt a stopped view.
export function createPreviewOwner() {
  let generation = 0;
  let active: number | null = null;
  let sessionId: string | null = null;
  return {
    begin(): number | null {
      if (active !== null) return null;
      active = ++generation;
      return active;
    },
    current(token: number) {
      return active === token;
    },
    adopt(token: number, id: string) {
      if (active !== token) return false;
      sessionId = id;
      return true;
    },
    stop() {
      const previous = sessionId;
      generation += 1;
      active = null;
      sessionId = null;
      return previous;
    },
  };
}
