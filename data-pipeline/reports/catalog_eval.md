# Catalog evaluation: dish profiles against reference recipes

_Generated 2026-10-26 19:15 UTC by `python -m pantrypath_pipeline eval-catalog`._

Each catalog dish lists the ingredients a shopper needs. We compare that list with a hand-written reference recipe for the same dish. **dev** = the 16 golden recipes we used while building the pipeline (optimistic). **test** = recipes written after the catalog was frozen, for dishes nobody checked by hand (held out).

| Split | Dishes | Recall | Recall without LLM additions | Core precision | Dishes fully covered |
|---|---|---|---|---|---|
| dev | 16 | 91.1% | 87.9% | 82.9% | 7 of 16 |
| test | 27 | 89.8% | 86.4% | 89.4% | 13 of 27 |

- *Recall*: share of the reference's required ingredients found in the dish profile.
- *Recall without LLM additions*: the same, counting only ingredients that came from Food.com.
- *Core precision*: share of the profile's core ingredients that the reference recipe uses.

## dev: per dish

| Dish | Found | Missed | Core not in the reference |
|---|---|---|---|
| Quiche Lorraine | 7/7 | – | swiss cheese |
| Pad Thai | 11/11 | – | garlic |
| Chicken Tikka Masala | 8/9 | chicken thighs | ground cumin, boneless skinless chicken breast |
| Beef Lasagna | 8/9 | marinara sauce | – |
| Pesto Pasta | 5/6 | spaghetti | basil pesto, pasta |
| Beef Stew with Red Wine | 10/10 | – | – |
| Shrimp Scampi | 8/8 | – | – |
| Vegetable Fried Rice | 5/8 | jasmine rice, frozen peas and carrots, sesame oil | long-grain white rice |
| Chocolate Chip Cookies | 8/8 | – | – |
| Caesar Salad | 10/10 | – | – |
| Chili con Carne | 8/9 | beef broth | – |
| Salmon Avocado Sushi Rolls | 5/6 | sushi-grade salmon | cucumber, imitation crab |
| Buttermilk Pancakes | 6/7 | buttermilk | milk |
| Hummus | 6/6 | – | – |
| Spaghetti Carbonara | 3/4 | pecorino romano cheese | parmesan cheese, bacon |
| Tuna Salad Sandwich | 5/6 | whole wheat bread | sandwich bread |

## test: per dish

| Dish | Found | Missed | Core not in the reference |
|---|---|---|---|
| Macaroni and Cheese | 5/5 | – | – |
| Banana Bread | 7/7 | – | – |
| Meatloaf | 7/8 | bread crumbs | breadcrumbs |
| Pot Roast | 7/8 | vegetable oil | – |
| Brownies | 7/7 | – | – |
| Chicken Pot Pie | 7/8 | frozen mixed vegetables | carrots |
| Potato Salad | 4/6 | red potatoes, red onion | potatoes |
| Pumpkin Pie | 8/8 | – | – |
| Cornbread | 7/7 | – | – |
| Coleslaw | 5/5 | – | – |
| Chicken Noodle Soup | 8/8 | – | – |
| Deviled Eggs | 4/4 | – | – |
| Beef Stroganoff | 8/9 | beef sirloin | beef stew meat |
| Baked Ziti | 6/7 | ziti | ziti pasta |
| Italian Meatballs | 8/9 | bread crumbs | italian breadcrumbs |
| Zuppa Toscana | 8/8 | – | – |
| Mexican Rice | 7/7 | – | – |
| Chicken Enchiladas | 7/7 | – | – |
| Steak Fajitas | 7/9 | bell peppers, vegetable oil | green bell pepper |
| Black Bean Soup | 9/9 | – | – |
| Kung Pao Chicken | 8/11 | chicken thighs, dried red chiles, vegetable oil | boneless skinless chicken breast, red pepper flakes |
| Egg Drop Soup | 6/7 | sesame oil | – |
| Beef and Broccoli Stir-Fry | 8/9 | flank steak | beef sirloin steak |
| Butter Chicken | 8/11 | chicken thighs, tomato sauce, turmeric | tomato puree, chicken breast |
| Naan | 6/6 | – | – |
| Falafel | 7/9 | baking soda, vegetable oil | – |
| Tabbouleh | 6/7 | parsley | flat-leaf parsley |

## Limits

- One reference recipe per dish; real recipes vary, so a "miss" can be a legitimate variant, and core precision is a lower bound.
- Name matching is rule-based (shared last word, differing only by size or preparation words, plus the synonym pairs in config). It was tuned on the dev split only. Held-out misses that are only naming differences ("breadcrumbs" vs "bread crumbs") are left as misses, so held-out recall is a lower bound.
- The reference recipes were drafted with an AI assistant and need a check by a teammate.
