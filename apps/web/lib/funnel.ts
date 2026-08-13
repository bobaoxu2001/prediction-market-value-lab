/**
 * The deliberately small vocabulary for PMVL's first product funnel.
 *
 * Keeping this list closed is the main privacy boundary. Callers cannot attach
 * arbitrary properties, URLs, market identifiers, order sizes, account data or
 * free-form text to an event: the route accepts one of these names and, for the
 * few events that need it, one non-identifying placement label.
 */
export const FUNNEL_EVENT_NAMES = [
  "extension_install_intent",
  "founding_offer_intent",
  "result_shared",
  "watchlist_added",
] as const;

export type FunnelEventName = (typeof FUNNEL_EVENT_NAMES)[number];

export const FUNNEL_PLACEMENTS = [
  "beta_zip",
  "chrome_web_store",
  "native_share",
  "clipboard",
  "pricing",
] as const;

export type FunnelPlacement = (typeof FUNNEL_PLACEMENTS)[number];

const EVENT_PLACEMENTS: Partial<Record<FunnelEventName, ReadonlySet<FunnelPlacement>>> = {
  extension_install_intent: new Set(["beta_zip", "chrome_web_store"]),
  founding_offer_intent: new Set(["pricing"]),
  result_shared: new Set(["native_share", "clipboard"]),
};

export interface FunnelEvent {
  name: FunnelEventName;
  source: "web";
  placement?: FunnelPlacement;
}

const EVENT_NAMES = new Set<string>(FUNNEL_EVENT_NAMES);
const PLACEMENTS = new Set<string>(FUNNEL_PLACEMENTS);

export function isFunnelEventName(value: unknown): value is FunnelEventName {
  return typeof value === "string" && EVENT_NAMES.has(value);
}

export function isFunnelPlacement(value: unknown): value is FunnelPlacement {
  return typeof value === "string" && PLACEMENTS.has(value);
}

export function validFunnelEvent(
  name: unknown,
  source: unknown,
  placement: unknown,
): Pick<FunnelEvent, "name" | "placement"> | null {
  if (!isFunnelEventName(name) || source !== "web") return null;
  if (placement !== undefined && !isFunnelPlacement(placement)) return null;
  const allowedPlacements = EVENT_PLACEMENTS[name];
  const valid = allowedPlacements
    ? allowedPlacements.has(placement as FunnelPlacement)
    : placement === undefined;
  return valid
    ? { name, ...(placement === undefined ? {} : { placement: placement as FunnelPlacement }) }
    : null;
}

/**
 * Parse only the exact public payload shape. Extra properties are rejected, not
 * silently dropped: accepting them would make it easy for a later caller to
 * believe sensitive context was part of the supported analytics contract.
 */
export function parseFunnelEvent(value: unknown): FunnelEvent | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;

  const input = value as Record<string, unknown>;
  const keys = Object.keys(input);
  if (keys.some((key) => !["name", "source", "placement"].includes(key))) return null;
  const event = validFunnelEvent(input.name, input.source, input.placement);
  if (!event) return null;

  return {
    name: event.name,
    source: "web",
    ...(event.placement === undefined ? {} : { placement: event.placement }),
  };
}
