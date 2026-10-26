# Bias report: data slicing

_Generated 2026-10-26 19:31 UTC by `python -m pantrypath_pipeline bias-report`. Every number and finding is recomputed on each run._

Pantry Path trains no model in this phase. Two parts of the pipeline make decisions that could serve some groups worse than others:

1. the **dish catalog**: which cuisines get dishes, and how much data backs each dish;
2. **diet_rules**, which marks every product *safe*, *unsafe* or *unknown* for a user. It is the "model" evaluated here, on slices of products.

**In one line:** Ingredient-text fallback and name/category checks cut diet_rules' unknown rate from 77% to 0.6% (false safe 0.0% → 0.4%); the widest gap left is by category group: bakery cereals 2.9% vs pantry condiments 0.1% unknown.

## Findings

- Catalog before mitigation: a plain top 100 by popularity is 74% American or untagged (1 tagged American, 73 with no cuisine keyword).
- Catalog after mitigation (cuisine quotas): 100 dishes, 60% American (target 60%).
- Every cuisine reached its quota.
- Data behind each dish: other cuisines rest on a median of 51 Food.com recipes (American: 127); 3.3% of their ingredients were added by the LLM (American: 3.8%).
- Coverage: on a fixed sample of 20,000 products, diet_rules could not decide 77% of verdicts from allergen tags alone; reading the ingredient text brings this to 0.6%, and the name/category checks to 0.6%.
- Dangerous errors: products known to be unsafe were marked safe 0.0% of the time with tags only, 2.2% with the ingredient text, and 0.4% with the full rules.
- Usefulness: the rules confirm 0% of labelled-safe products as safe before (tags alone never prove a product safe) and 96% after; when they decide, they match the labels 100% → 99% of the time.
- By category group, the unknown rate differs by up to 2.8 pts between groups with the full rules (bakery cereals 2.9% vs pantry condiments 0.1%), narrower than the 46 pts gap with tags only; in bakery cereals the gluten free user is affected most (8.4% unknown).
- By brand type (store vs national), the unknown rate differs by up to 0.1 pts between groups with the full rules (national brand 0.7% vs store brand 0.5%), narrower than the 3.0 pts gap with tags only; in national brand the gluten free user is affected most (1.9% unknown).
- By popularity (Open Food Facts scans), the unknown rate differs by up to 1.3 pts between groups with the full rules (10+ scans 1.8% vs 0 scans 0.6%), narrower than the 5.6 pts gap with tags only; in 10+ scans the gluten free user is affected most (5.5% unknown).
- Highest false-safe slice with the full rules: pantry condiments (category group), 1.7% of 1,235 unsafe verdicts.
- Held out: 0 labelled products added since the seen list was frozen on 2026-10-26; too few to score yet (the set grows weekly).

## 1. Dish catalog by cuisine

Before: Food.com cuisine keywords for a plain top 100 by popularity (untagged = no cuisine keyword). After: the LLM's cuisine labels in the quota catalog (`catalog.quotas`, the mitigation).

| Cuisine | Plain top 100 (before) | Quota | Catalog (after) |
|---|---|---|---|
| american | 1 (1%) | 60 | 60 (60%) |
| european | 13 (13%) | 10 | 10 (10%) |
| latin american | 7 (7%) | 10 | 10 (10%) |
| east se asian | 4 (4%) | 10 | 10 (10%) |
| south asian | 1 (1%) | 5 | 5 (5%) |
| middle eastern african | 1 (1%) | 5 | 5 (5%) |
| untagged | 73 (73%) | – | – |

Data behind each dish (Fairlearn `MetricFrame`, sensitive feature = cuisine):

| Cuisine | Dishes | Ingredients per dish | Core per dish | Added by LLM | Median recipes | Median reviews |
|---|---|---|---|---|---|---|
| american | 60 | 10.8 | 4.3 | 3.8% | 127 | 792 |
| european | 10 | 11.7 | 4.2 | 4.0% | 68 | 849 |
| latin american | 10 | 13.1 | 3.8 | 2.2% | 93 | 548 |
| east se asian | 10 | 13.6 | 5.1 | 6.7% | 43 | 338 |
| south asian | 5 | 15.4 | 4.6 | 0.9% | 45 | 521 |
| middle eastern african | 5 | 12.4 | 5.0 | 0% | 48 | 142 |

Largest gaps between cuisines: 4.6 ingredients per dish, 6.7 pts in LLM-added share, 84 in median source recipes.

## 2. diet_rules across product slices

### How it was measured

- **Data:** the Open Food Facts US snapshot (410,009 products with ingredient or allergen data). Coverage uses a fixed sample of 20,000 products; correctness uses 40,000 products that may have a ground truth (seed 42: the same products every run). This is a fixed sample, not a held-out test set (see Limitations); the held-out check below scores only products the rules were never tuned on.
- **Users:** vegan, vegetarian, and avoiding gluten.
- **Ground truth (evaluation only, never read by the rules):** safe = the product carries the claim (`en:vegan`; `en:vegetarian` or `en:vegan`; `en:no-gluten`). Unsafe = a clear fact: an animal category (meat, fish, seafood; for vegans also dairy, eggs, honey) or the gluten allergen tag. A fact contradicted by a claim or by the product name or brand ("veggie burger" under meats, "gluten-free" with a gluten tag) is a conflict and left out; categories for plant-based versions (substitutes, alternatives, vegan, …) never count as animal.
- **Before / after:** *tags only* = allergen and trace tags only (before); *+ text* = tags + ingredient text; *full* = tags + ingredient text + name and category checks (after, used by the pipeline).
- **Metrics:** *unknown* = share the rules can't decide (never shown as safe); *accuracy* = agreement with the ground truth when decided; *false safe* = products known to be unsafe marked safe (the dangerous error); *safe found* = labelled-safe products confirmed safe.
- Groups with fewer than 30 verdicts are marked * and left out of gap numbers.

| User | Safe (label claim) | Unsafe (fact) | Conflicts left out |
|---|---|---|---|
| vegan | 2,211 | 10,927 | 188 |
| vegetarian | 2,355 | 4,591 | 93 |
| gluten free | 5,536 | 23,721 | 203 |

### Overall

Each cell: tags only → + ingredient text → full rules.

| User | Unknown (all products) | Accuracy when decided | False safe | Safe found |
|---|---|---|---|---|
| vegan | 62% → 0.0% → 0.0% | 99% → 96% → 98% | 0.0% → 4.1% → 1.1% | 0% → 95% → 95% |
| vegetarian | 92% → 0.0% → 0.0% | 100% → 94% → 99% | 0.0% → 8.5% → 0.2% | 0% → 99% → 98% |
| gluten free | 77% → 1.8% → 1.8% | 100% → 100% → 100% | 0.0% → 0.1% → 0.1% | 0% → 95% → 96% |
| all users | 77% → 0.6% → 0.6% | 100% → 98% → 99% | 0.0% → 2.2% → 0.4% | 0% → 96% → 96% |

### Held-out check: products added after the rules were frozen

diet_rules was tuned on the products whose 411,910 barcodes were frozen as seen (last freeze 2026-10-26). Products a later snapshot adds were never looked at, so the labelled ones among them are a true held-out test. The set starts empty and grows with every weekly snapshot.

**0 held-out products in this snapshot** (it is the frozen one).

### Coverage by slice (unknown rate, all users)

**By category group**

| Group | Products | Unknown: tags → + text → full |
|---|---|---|
| plant based substitutes | 170 | 85% → 1.8% → 1.8% |
| meat seafood | 920 | 60% → 0.1% → 0.1% |
| dairy eggs | 1,160 | 70% → 0.3% → 0.3% |
| meals | 773 | 51% → 0.2% → 0.2% |
| snacks sweets | 5,053 | 71% → 0.8% → 0.8% |
| bakery cereals | 1,334 | 69% → 2.9% → 2.9% |
| beverages | 1,598 | 97% → 0.2% → 0.2% |
| pantry condiments | 2,113 | 90% → 0.1% → 0.1% |
| produce legumes nuts | 935 | 98% → 0.2% → 0.2% |
| other | 1,176 | 69% → 0.7% → 0.7% |
| uncategorized | 4,768 | 78% → 0.5% → 0.5% |

**By brand type (store vs national)**

| Group | Products | Unknown: tags → + text → full |
|---|---|---|
| store brand | 2,008 | 77% → 0.5% → 0.5% |
| national brand | 11,791 | 78% → 0.7% → 0.7% |
| no brand | 6,201 | 75% → 0.6% → 0.6% |

**By popularity (Open Food Facts scans)**

| Group | Products | Unknown: tags → + text → full |
|---|---|---|
| 0 scans | 15,609 | 76% → 0.6% → 0.6% |
| 1-9 scans | 4,228 | 79% → 0.8% → 0.8% |
| 10+ scans | 163 | 81% → 1.8% → 1.8% |

### Correctness by slice (labelled products, all users)

Each cell: tags only → full rules.

**By category group**

| Group | Verdicts | Accuracy | False safe | Safe found |
|---|---|---|---|---|
| plant based substitutes | 562 | 98% → 99% | 0.0% → 0.0% | 0% → 97% |
| meat seafood | 10,065 | 100% → 100% | 0.0% → 0.2% | 0% → 99% |
| dairy eggs | 6,569 | 100% → 99% | 0.0% → 1.5% | 0% → 97% |
| meals | 2,599 | 100% → 99% | 0.0% → 0.0% | 0% → 94% |
| snacks sweets | 10,381 | 100% → 99% | 0.0% → 0.1% | 0% → 96% |
| bakery cereals | 5,267 | 100% → 100% | 0.0% → 0.0% | 0% → 92% |
| beverages | 632 | 96% → 99% | 0.0% → 0.0% | 0% → 98% |
| pantry condiments | 2,255 | 99% → 98% | 0.0% → 1.7% | 0% → 98% |
| produce legumes nuts | 442 | 98% → 99% | 0.0% → 0.0% | 0% → 99% |
| other | 2,923 | 99% → 99% | 0.0% → 0.1% | 0% → 95% |
| uncategorized | 7,646 | 100% → 99% | 0.0% → 0.0% | 0% → 96% |

**By brand type (store vs national)**

| Group | Verdicts | Accuracy | False safe | Safe found |
|---|---|---|---|---|
| store brand | 5,360 | 100% → 100% | 0.0% → 0.3% | 0% → 98% |
| national brand | 29,442 | 100% → 99% | 0.0% → 0.5% | 0% → 96% |
| no brand | 14,539 | 100% → 100% | 0.0% → 0.3% | 0% → 96% |

**By popularity (Open Food Facts scans)**

| Group | Verdicts | Accuracy | False safe | Safe found |
|---|---|---|---|---|
| 0 scans | 35,278 | 100% → 99% | 0.0% → 0.3% | 0% → 96% |
| 1-9 scans | 13,342 | 99% → 99% | 0.0% → 0.6% | 0% → 96% |
| 10+ scans | 721 | 97% → 98% | 0.0% → 0.7% | 0% → 96% |

### Largest gaps between slices

Fairlearn `MetricFrame.difference()` (max - min across groups), tags only → full rules; the groups named are the extremes with the full rules.

| Slicing | Unknown rate | Accuracy when decided | False safe |
|---|---|---|---|
| category group | 46 pts → 2.8 pts (bakery cereals 2.9% vs pantry condiments 0.1%) | 3.5 pts → 1.4 pts (bakery cereals 100% vs pantry condiments 98%) | 0.0 pts → 1.7 pts (pantry condiments 1.7% vs bakery cereals 0%) |
| brand type (store vs national) | 3.0 pts → 0.1 pts (national brand 0.7% vs store brand 0.5%) | 0.3 pts → 0.4 pts (store brand 100% vs national brand 99%) | 0.0 pts → 0.2 pts (national brand 0.5% vs store brand 0.3%) |
| popularity (Open Food Facts scans) | 5.6 pts → 1.3 pts (10+ scans 1.8% vs 0 scans 0.6%) | 3.3 pts → 1.3 pts (0 scans 99% vs 10+ scans 98%) | 0.0 pts → 0.4 pts (10+ scans 0.7% vs 0 scans 0.3%) |

### Disagreements: ground truth unsafe, rules say safe

First five per user with the full rules (same every run). Some are real misses (leads for diet_rules), some are wrong categories or tags in Open Food Facts.

| User | Product | Brand | Group | Barcode |
|---|---|---|---|---|
| vegan | zuzu iaurt fructe padure | zuzu | dairy eggs | 5941355008844 |
| vegan | Kvarg vanilla | Lindahls | dairy eggs | 0025990018415 |
| vegan | Organic amber agave nectar | Cadia | pantry condiments | 0815369012171 |
| vegan | Fruit Bars Strawberry Flavored | Publix | dairy eggs | 0041415481435 |
| vegan | Garlic chive chevre and cracker sticks | – | dairy eggs | 0861140000325 |
| vegetarian | Classic Meatloaf | Field Roast | meat seafood | 0638031615216 |
| vegetarian | Classic burger patties, classic | Upton's Naturals Co | meat seafood | 0851070006085 |
| vegetarian | Kombu | Rising Tide Sea Vegetables | meat seafood | 0630614000044 |
| vegetarian | Worthington, Loma Linda, Veja-Links Vegetable & Protein Grain Links | Atlantic Natural Foods Llc | meat seafood | 0845561000744 |
| vegetarian | Roasted Laver with Olive Oil | Choripdong | meat seafood | 0761898647554 |
| gluten free | Kids dha omega-3s bars | – | snacks sweets | 0816926020066 |
| gluten free | Kids cinnamon roll bars | – | snacks sweets | 0816926020080 |
| gluten free | Pocket sandwich | Glutenfreeda Foods  Inc. | other | 0858246001714 |
| gluten free | Chocolate Chip Mini Crunchy Cookies | Partake | uncategorized | 0850032528696 |
| gluten free | Crunchy Lemon Cookies | Hungryroot | snacks sweets | 0810062522286 |

### Disagreements: labelled safe, rules say unsafe

First five per user. Many are label errors: products claiming vegan or gluten-free while listing eggs, chicken stock or wheat.

| User | Product | Brand | Group | Barcode |
|---|---|---|---|---|
| vegan | Vegan protein | Sprouts | uncategorized | 0646670681851 |
| vegan | chocolate chip cookie dough protein bar | no cow | snacks sweets | 0852346005962 |
| vegan | CUP'N CRISP PEPPERONI | Hormel | uncategorized | 0037600883214 |
| vegan | Mealtime for Baby Beef and gravy | Gerber | other | 0000075985271 |
| vegan | snack nut bars | Meijer | snacks sweets | 0713733286565 |
| vegetarian | Vegan protein | Sprouts | uncategorized | 0646670681851 |
| vegetarian | Chicken Curry Masala | Aachi | pantry condiments | 8906021120487 |
| vegetarian | CUP'N CRISP PEPPERONI | Hormel | uncategorized | 0037600883214 |
| vegetarian | Mealtime for Baby Beef and gravy | Gerber | other | 0000075985271 |
| vegetarian | Bison Meatloaf | Crave New World | uncategorized | 0860013066000 |
| gluten free | IntraMax | druckerlabs | other | 0819246001043 |
| gluten free | the Better Muffin Bar Soft Baked Oat Bars Banana Bread | Member's Mark | uncategorized | 0193968301163 |
| gluten free | Dark + White Chocolate Freeze-Dried Raspberries | tru fru | uncategorized | 0850048358263 |
| gluten free | Fiber Powder | – | uncategorized | 0053041410628 |
| gluten free | Polpa de Coco 100g | Fruta Mil | uncategorized | 7897571500687 |

## 3. Mitigation and trade-offs

- **Catalog: cuisine quotas** (`catalog.quotas`). The catalog is 60% American, down from 74% American or untagged in a plain top 100. Trade-off: the quotas pull in dishes backed by fewer source recipes (median 51 vs 127 for American dishes), so their ingredient shares rest on less data.
- **diet_rules: ingredient-text fallback and name/category checks.** The unknown rate drops from 77% to 0.6% and labelled-safe products confirmed as safe rise from 0% to 96%. Trade-off: deciding more products costs some safety: the false-safe rate rises from 0.0% to 0.4% (4 more per 1,000 unsafe products). Ingredient text alone would mark 2.2% of unsafe products safe (e.g. cure-only bacon lists no meat); the name/category check brings this down to 0.4%.
- **Not applied:** re-sampling, re-weighting and threshold tuning are levers for trained models; diet_rules has no training data and no score threshold. The equivalent safeguard is kept instead: unknown is never shown as safe, so a slice with more unknowns gets fewer suggestions, never less safe ones.

## 4. Limitations

- **Not a held-out evaluation.** While diet_rules was being built, its keyword rules were adjusted after looking at mismatches with these same Open Food Facts vegan, vegetarian and gluten-free labels on a ~47k-product sample of this snapshot (for example, recall on vegan-labelled products rose from 91.3% to 92.7%). The fixed sample used here overlaps that data, so accuracy and safe-found numbers are likely optimistic. The fair estimate is the held-out check: products added to Open Food Facts after the snapshot the rules were tuned on (its barcodes are frozen in `bias.seen_products`). It starts empty and grows with each weekly snapshot; rerun `freeze-seen-products` whenever the rules are changed after looking at newer products, or the check stops being held out.
- **Label noise.** Open Food Facts claims are not always right: some products labelled vegan or gluten-free list eggs, chicken stock or wheat (see the disagreement tables). Such products count against the rules when the rules are actually right, and a category or tag error does the same in the other direction.
- Labels are self-reported packaging claims entered by Open Food Facts contributors. Brands that market to vegan or gluten-free shoppers carry them more often, so the labelled set is not a random sample of products.
- "Unsafe" is a proxy. Open Food Facts categories and tags have errors (meat-free burgers filed under meats); products whose name or brand contradicts the fact are left out, but some errors remain, so part of the false-safe rate is label noise. Gluten negatives come from the same allergen tags the rules read, so every mode scores close to perfect on them by construction. Vegan and vegetarian negatives come from categories, which only the full mode reads: its false-safe numbers on them are a lower bound.
- The snapshot only keeps products with ingredient or allergen data, and store products missing from Open Food Facts are not in it, so unknown rates at request time are higher.
- Store vs national brand uses a hand-made list of US store brands (`bias.store_brands`); regional store brands count as national.
- Popularity is Open Food Facts scans: a proxy for how common a product is, not sales.
- Catalog: "before" uses Food.com cuisine keywords, "after" the LLM's cuisine labels; untagged Food.com dishes were mostly American when checked by hand.
