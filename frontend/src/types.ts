// Shapes the backend sends (backend/app/schemas.py). Result follows the result.json
// contract v1.0 (docs/result-v1.schema.json); Answer is A3's answer (ml/src/pantrypath_ml/answer.py).

export type Diet = "none" | "vegetarian" | "vegan";
export type Verdict = "safe" | "unsafe" | "unknown";

export interface Option {
  id: string;
  label: string;
}

/** "Not right? Ask a stronger model" (backend/app/stronger_model.py): off without a key. */
export interface StrongerModel {
  available: boolean;
  label: string | null; // e.g. "Gemini"
}

/** Who worked out a result's ingredients. a2 is null for a dish from the catalog. */
export interface ModelUsed {
  a2: string | null;
  stronger: boolean;
}

export interface AppConfig {
  mode: "demo" | "live";
  engine: { a3_available?: boolean; examples?: string[]; [key: string]: unknown };
  stronger_model?: StrongerModel;
  allergens: Option[];
  diets: Option[];
  needs: Option[];
  result_ttl_minutes: number;
}

/** What a user saves once (their "profile"), so the chat doesn't have to ask every time. */
export interface Preferences {
  name: string;
  zip: string | null;
  avoid: string[];
  diet: Diet;
  servings: number | null;
  needs: string[];
  /** Favourite brand per item ("sandwich bread" -> "Dave's Killer Bread"). The
   * preferences form never sends it, so a save keeps it. */
  favourites?: Record<string, string>;
}

/** The logged-in account. The API never sends a password or its hash. */
export interface User extends Preferences {
  id: string;
  username: string;
  created_at: string;
  updated_at: string;
}

export interface SearchRequest {
  query: string;
  zip: string;
  avoid: string[];
  diet: Diet;
}

export type Source = "chat" | "profile" | "both" | null;

/** A confirm card: the request read from the chat and the saved preferences. */
export interface Draft {
  state: "ready" | "check";
  request: SearchRequest;
  servings: number | null;
  sources: { avoid: Record<string, Source>; diet: Source; zip: Source };
  locked: string[];
  min_diet: Diet;
  issues: { kind: string; text: string }[];
  notes: string[];
  needs: string[];
  in_catalog: boolean | null;
  submitted?: boolean;
  /** At the end of a guided build: the ingredients chosen, searched as they are. */
  guided?: GuidedSearch | null;
}

export interface GuidedIngredient {
  name: string;
  category: string;
  role: "core" | "common";
}

export interface GuidedSearch {
  dish: string;
  cuisine: string;
  source: "catalog" | "template" | "model";
  ingredients: GuidedIngredient[];
  added: string[];
  choices?: Record<string, ProductView>; // the picked product of each item
  favourites_new?: Record<string, string>; // kept when the search starts
  favourites_ask?: { item: string; old: string; new: string }[]; // "make it your usual?"
}

/** A product as a build offers it (backend/app/choices.py); memory only, never saved. */
export interface ProductView {
  key: string;
  name: string;
  brand: string;
  size: string;
  aisle: string | null;
  verdict: Verdict;
  reason: string;
  store_id: string;
  store: string;
  miles: number | null;
  other_store: boolean;
}

export interface ProductChoice {
  pick: ProductView | null;
  alternatives: ProductView[];
  hint?: ProductView | null; // a favourite brand of the same family: offered first, not picked
  hint_brand?: string | null;
  favourite?: string | null;
  favourite_missing?: boolean;
  hidden_unsafe?: number;
  error?: string | null;
}

/** One option of a build step (backend/app/plans.py). */
export interface BuildOption {
  name: string;
  category: string;
  usual: boolean;
  share: number | null; // "in 72% of recipes": catalog dishes only
  instead_of: string | null; // a swap for an option left out
}

export interface BuildStep {
  id: string;
  label: string;
  options: BuildOption[];
  left_out: { name: string; reason: string }[];
}

export interface Clash {
  name: string;
  allergens: string[];
  diet: string | null;
  swap: string | null;
  reason: string;
}

export interface Addition {
  name: string;
  status: "ok" | "clash" | "anyway";
  clash: Clash | null;
  instead_of?: string;
}

/** A guided build card (backend/app/guided.py). */
export interface BuildData {
  plan: { dish: string; cuisine: string; source: "catalog" | "template" | "model"; steps: BuildStep[] };
  step: number; // steps.length once done
  picks: Record<string, string[]>;
  additions: Addition[];
  state: "choosing" | "done" | "searched" | "cancelled";
  draft: Draft;
  products: { status: "loading" | "ready" | "failed"; main: string | null; stores: Store[]; error: string | null };
  choices: Record<string, ProductChoice>;
  chosen: Record<string, string>;
}

export type BuildAction =
  | { action: "next" | "back" | "finish" | "usual"; picks?: string[] }
  | { action: "add"; text: string }
  | { action: "resolve"; name: string; choice: "swap" | "anyway" | "remove" }
  | { action: "choose"; name: string; key: string }
  | { action: "cancel" };

export interface SearchData {
  job_id: string;
  request: SearchRequest;
  extras: { servings?: number | null };
  status: "running" | "done" | "failed";
  dish?: string;
  error?: string | null;
  error_kind?: string | null;
  stronger?: boolean; // searched again with the stronger model
  redo_of?: string; // the search message it redoes
  guided?: GuidedSearch | null; // the ingredients a guided build chose
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  kind: "text" | "build" | "confirm" | "search" | "error";
  text: string;
  data: Record<string, unknown> | null;
  created_at: string;
}

export interface Session {
  id: string;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  context_start: number;
  messages: Message[];
}

export interface SessionSummary {
  id: string;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  preview: string;
}

export interface Store {
  store_id: string;
  chain: string;
  name: string;
  address: string;
  lat: number;
  lon: number;
  distance_miles: number;
}

export interface Product {
  store_id: string;
  barcode: string | null;
  name: string;
  brand: string;
  size: string;
  in_stock: boolean | null;
  aisle: string | null;
  verdict: Verdict;
  reason: string;
  evidence_source: "kroger" | "target" | "off" | "none";
}

export interface IngredientResult {
  name: string;
  role: "core" | "common";
  category: string;
  products: Product[];
}

export interface Result {
  request: SearchRequest;
  dish: { name: string; cuisine: string; source: "catalog" | "llm" };
  stores: Store[];
  ingredients: IngredientResult[];
  summary: { ingredients_total: number; with_safe_product: number; without_safe_product: string[] };
  meta: {
    generated_at: string;
    expires_at: string;
    providers_used: string[];
    catalog_version: string;
    schema_version: string;
  };
}

export interface AnswerItem {
  name: string;
  status: "buy" | "substitute" | "check_label" | "skip";
  product: string | null;
  store_id: string | null;
  substitute: string | null;
  quantity: number | null;
  unit: string | null;
  note: string | null;
}

export interface Answer {
  summary: string;
  servings: number | null;
  ingredients: AnswerItem[];
  steps: string[];
}

export interface Job {
  id: string;
  status: "running" | "done" | "failed";
  stage: "queued" | "searching" | "answering" | "done" | "failed";
  error: string | null;
  error_kind: string | null;
  created_at: string;
  finished_at: string | null;
  expires_at: string | null;
  result: Result | null;
  answer: Answer | null;
  answer_status: string | null;
  warnings: string[];
  model_used?: ModelUsed | null;
  list_id?: string | null; // the saved list this search made (My lists)
  list_error?: boolean; // saving it failed
  choices?: Record<string, ProductView> | null; // the shopper's picked products (a guided build)
}

/** One item of a saved list (backend/app/lists.py): the shopper's data, never Kroger's. */
export interface SavedItem {
  id: string;
  name: string;
  category: string;
  status: "buy" | "substitute" | "check_label" | "skip";
  substitute: string | null;
  quantity: number | null;
  unit: string | null;
  ticked: boolean;
  brand?: string | null; // the brand the shopper picked
}

export interface SavedList {
  id: string;
  dish: string;
  cuisine: string;
  zip: string;
  avoid: string[];
  diet: Diet;
  servings: number | null;
  items: SavedItem[];
  steps: string[];
  source: string;
  created_at: string;
  updated_at: string | null;
}

export interface SavedListSummary {
  id: string;
  dish: string;
  created_at: string;
  items: number;
  ticked: number;
}

export interface ChatReply {
  session: Session;
  job?: Job | null;
}
