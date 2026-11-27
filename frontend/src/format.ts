import type { AnswerItem, Option, Product, Store, Verdict } from "./types";

export const cap = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

export function labelOf(options: Option[] | undefined, id: string): string {
  return options?.find((o) => o.id === id)?.label ?? cap(id.replace(/_/g, " "));
}

export function joinWords(words: string[]): string {
  if (words.length <= 2) return words.join(" and ");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

export function relativeTime(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  safe: "Safe",
  unsafe: "Not safe",
  unknown: "Couldn't verify",
};

export const EVIDENCE_LABEL: Record<Product["evidence_source"], string> = {
  kroger: "Kroger product data",
  target: "Target product page",
  off: "Open Food Facts",
  none: "No label data",
};

export const CUISINE_LABEL: Record<string, string> = {
  american: "American",
  european: "European",
  latin_american: "Latin American",
  east_se_asian: "East & Southeast Asian",
  south_asian: "South Asian",
  middle_eastern_african: "Middle Eastern & African",
};

export function quantityText(item: AnswerItem): string {
  if (item.quantity == null) return "";
  const amount = Number.isInteger(item.quantity) ? String(item.quantity) : item.quantity.toFixed(2).replace(/0+$/, "");
  return item.unit ? `${amount} ${item.unit}` : amount;
}

export function milesText(miles: number): string {
  return `${miles.toFixed(1)} mi`;
}

/** Google Maps directions from the shopper's ZIP through the stores, nearest first. */
export function routeLink(originZip: string, stores: Store[]): string | null {
  if (!stores.length) return null;
  const stops = [...stores].sort((a, b) => a.distance_miles - b.distance_miles);
  const point = (s: Store) => `${s.lat},${s.lon}`;
  const params = new URLSearchParams({
    api: "1",
    origin: originZip,
    destination: point(stops[stops.length - 1]),
    travelmode: "driving",
  });
  if (stops.length > 1) params.set("waypoints", stops.slice(0, -1).map(point).join("|"));
  return `https://www.google.com/maps/dir/?${params.toString()}`;
}

export function storeLink(store: Store): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${store.name}, ${store.address}`)}`;
}

/** Kroger's aisle text often repeats itself ("Aisle 1, aisle 1"): show each part once. */
export function aisleText(aisle: string | null | undefined): string {
  if (!aisle) return "";
  const parts: string[] = [];
  for (const part of aisle.split(/\s*,\s*/)) {
    if (part && !parts.some((p) => p.toLowerCase() === part.toLowerCase())) parts.push(part);
  }
  return parts.join(", ");
}
