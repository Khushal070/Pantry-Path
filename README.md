# Pantry Path

A conversational food finder for people with special diets. You say what you want to eat ("I feel like quiche tonight"). The assistant adapts the recipe to your diet, searches nearby stores while you chat, and returns:

1. A shopping list: the product and package to buy for each ingredient, and how many.
2. A store list showing how strong the evidence is for each item: in stock at a store, a catalog match only, or just a nearby store.
3. The fastest multi-stop route through those stores.

Built by Project Team 11.

> Early development. The commands under Installation and Usage describe the planned setup and will be updated as the code lands.

## How it works

- A self-hosted open-weight language model reads the conversation and adapts the recipe to your diet.
- Deterministic diet rules, not the model, decide whether a product fits a diet. Unknown evidence is never shown as safe.
- Searches start with the first message, and everything found is cached for the session, so changing your mind does not repeat any search.
- One preloaded demo profile, no login or payments, and live data only.

Diets covered: allergies, celiac, pregnancy, medical (for example low-sodium or diabetic), religious (for example halal or kosher) and lifestyle (for example vegan).

## Tech stack

- Frontend: React
- Backend: FastAPI (Python)
- Model serving: vLLM or Ollama, on a GPU that scales to zero when idle
- LLMOps: DVC for data versioning, MLflow for the model and prompt registry
- Cloud: Google Cloud (for example Cloud Run)

## Data

| Source | Used for | License or terms |
|---|---|---|
| RecipeNLG | Model evaluation and fine-tuning examples | Research and educational use |
| Food.com Recipes and Interactions (Kaggle) | Diet-adaptation examples | Research use |
| Open Food Facts | Ingredients, allergens, labels | ODbL |
| USDA FoodData Central | Nutrition for medical diets | Public domain (CC0) |
| Kroger Products and Locations API | Branch-level stock and price | Kroger developer terms |
| OpenStreetMap (Overpass, Nominatim, OSRM) | Stores, geocoding, routing | ODbL |

Large datasets are not stored in git. They are versioned with DVC.

## Repository structure

- `backend/` - FastAPI service: chat orchestrator, provider adapters, diet rules engine, cache, package matcher, route optimizer
- `frontend/` - React chat app
- `ml/` - prompts, model and serving configuration, evaluation and fine-tuning scripts
- `data/` - diet rules, demo personas, golden evaluation sets, DVC pointers
- `pipelines/` - recipe dataset preparation and Open Food Facts / USDA import scripts
- `evals/`, `tests/` - model evaluations; rule, contract and conversation tests
- `infra/` - GCP deployment configuration
- `docs/` - project plan and architecture diagrams

## Installation

Prerequisites: Python 3.11+, Node.js 20+ and Git. For the model, Ollama for local development, or a GPU machine with vLLM.

1. Clone the repository:
   ```sh
   git clone https://github.com/Khushal070/Pantry-Path.git
   cd Pantry-Path
   ```
2. Copy `.env.example` to `.env` and fill in the values (Kroger API credentials, model server URL).
3. Install the backend:
   ```sh
   cd backend
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. Install the frontend:
   ```sh
   cd frontend
   npm install
   ```
5. Get the default model (set in `.env.example`) for local development:
   ```sh
   ollama pull qwen2.5:7b-instruct
   ```
6. Get the versioned datasets, once the DVC remote is set up:
   ```sh
   dvc pull
   ```

## Usage

1. Start the model server: `ollama serve` (or the vLLM server on a GPU machine).
2. Start the backend: `cd backend && uvicorn app.main:app --reload`. The API runs at http://localhost:8000.
3. Start the frontend: `cd frontend && npm run dev`, then open the URL it prints (http://localhost:5173 by default).
4. The demo profile loads automatically. Type what you want to eat, for example "I feel like quiche tonight", answer the assistant's questions and confirm the recipe. You get the shopping list, the store list and the route.
5. Run the tests with `pytest`. Model evaluations live in `evals/`.

## Team

Ansh Manoj Kanungo, Kenil Prafulbhai Devani, Khushal Bharatkumar Trivedi, Shivu Donmardi Gorva, Tirth Ghanshyambhai Borasaniya

## Disclaimer

Pantry Path gives information, not medical or religious advice. Always check product labels before buying.
