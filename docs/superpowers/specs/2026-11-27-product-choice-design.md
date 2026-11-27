# Choosing products, favourite brands and steps that match: design

Date: 2026-11-27. Status: approved (approach 1) and built (2026-11-27). Builds on guided dish
building and saved lists (both 2026-11-27).

## 1. Why

The final answer should be the shopper's own products, each with its label check, store
and aisle, not a list of ingredient names. Shoppers have brands they always buy; those
should be found directly next time, and only the rest asked about. The recipe steps must
use what was chosen (a quiche list with milk had steps saying "coconut cream").

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Where products are chosen | **In the build's steps, one round**: each kind shows the shopper's favourite first, then the nearest safe products |
| Q2 | When a pick becomes a favourite | **Automatically**; picking a different brand for the same item **asks before replacing** |
| Q3 | How far a favourite reaches | **The exact item is filled in**; the brand is **shown first, not ticked,** for items of the same family (Tillamook cheddar → Tillamook swiss offered first) |
| Q4 | Stores | **One main store when possible** (most usual items with a safe product, nearest on a tie), another store only where the main one has nothing safe; recomputed every search, never saved |
| Approach | | **1**: every option's products fetched up front, so every step after the first is instant |
| Steps | | Checked against the final list; one retry naming the problem; then repaired by code |

## 3. Storage (Kroger's terms)

Saved (the shopper's preference): **item → brand**, e.g. "sandwich bread → Dave's Killer
Bread", in the encrypted profile; a saved list's item keeps its brand too. Never saved:
Kroger's product record (product name, id, size, price, stock, aisle, store). Product
choices with their store and aisle live in the chat (memory) and the search job (memory)
only. The team should confirm this split against Kroger's developer agreement.

## 4. Flow

1. **Dish read** → plan (template, catalog or model, as before) → the stores near the ZIP
   and the products of **every option** and of the shopper's favourite brands
   ("Dave's Killer Bread sandwich bread") and family hints are fetched in the background,
   Kroger only (Target costs credits), judged by the diet rules. The build card shows
   "Finding products…" until they arrive (a few seconds), then:
2. **Main store**: the store with a safe product for the most usual options; nearest on a
   tie.
3. **Each step**: every option (kind) with its pick: the favourite if found and not unsafe,
   else the first safe product at the main store, else the nearest other store's safe
   product ("only at Bellevue Kroger"); "Change" shows up to 3 alternatives (safe first,
   then "read the label"; unsafe ones hidden, counted). A family hint is listed first, not
   picked.
4. **Additions** are searched when added and get a pick the same way; a named product is
   searched by its name.
5. **Summary card**: each chosen product with brand, store, aisle and verdict. New
   favourites (items without one) are saved when the search starts; a different brand for
   an item that has one asks "Make X your usual Y instead of Z?".
6. **Final search**: the chosen items; A3 sees only the chosen product of each item, so
   it buys exactly that one (or says read the label / substitute when the chosen product
   isn't safe). The result's job carries the choices, so the list shows "Your pick" with
   store and aisle. Steps are checked (section 5).
7. **Saved list** keeps item + brand; **Find products again** searches "brand item" first.

## 5. Steps that match the list

After A3: every step is scanned for foods from a vocabulary (every catalog ingredient,
every option the build offered) that are not on the final list (an item, its substitute,
or part of one: "cheese" is part of "swiss cheese"); salt, pepper and water are always
allowed. Any stray → A3 once more with "Use only these ingredients in the steps: …; don't
mention …" in its requests slot. Still stray → an unchosen option is replaced by the
chosen one(s) of the same build step; a step naming any other stray food is dropped.

## 6. Units

| Unit | Does |
|---|---|
| `engine.options(request, ingredients)` | Judged products for many items, Kroger only, no A3 (live: `result_for_ingredients`; demo: synthetic) |
| `app/choices.py` | Main store, per-option picks and alternatives, favourite and family-hint terms, product keys |
| `app/stepcheck.py` | The steps check and repair |
| `guided.py` / `chat.py` | Background options job per build, product picks, additions, summary with choices, favourite updates |
| `auth.py` / `schemas.py` | `favourites: {item: brand}` in the profile; `PUT /api/me/favourites` |
| Frontend | Build card with products and Change, summary with products and favourite questions, "Your pick" in the list, favourites in Preferences |

## 7. Out of scope

Prices; a saved favourite store; quantities per product; evaluating the plan prompt.

## 8. Notes from building (2026-11-27)

- The steps check's vocabulary needed A3's substitutes table too: the stray was
  "coconut cream", which the catalog doesn't have (it has coconut milk).
- Loose produce isn't remembered as a favourite: Kroger labels it with "brands" like
  "Fresh Tomatoes".
- Open: an item Kroger doesn't carry (e.g. "smoked mushrooms") makes the final search try
  Target (RedCircle); when RedCircle fails, its retries add about 50 s. Decide whether
  guided searches should skip the Target fallback for items the build already found
  nothing for.
