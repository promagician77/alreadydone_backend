# ALREADY Backend

FastAPI backend for **ALREADY** — voice cloning and stories (ElevenLabs).

## Setup

1. **Create a virtual environment** (recommended):
   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   # source .venv/bin/activate  # macOS/Linux
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure ElevenLabs**:
   - Copy `.env.example` to `.env`
   - Set `ELEVENLABS_API_KEY` to your [ElevenLabs](https://elevenlabs.io) API key

## Run

```bash
uvicorn app.main:app --reload
```

- API: http://127.0.0.1:8000  
- Docs: http://127.0.0.1:8000/docs  

## Voice cloning

**POST** `/api/voice/clone`

Creates an instant voice clone from uploaded audio using ElevenLabs.

- **Form fields**: `name` (required), `remove_background_noise` (optional, default `false`)
- **Files**: one or more audio files (e.g. MP3, WAV, M4A)

**Example (curl)**:
```bash
curl -X POST "http://127.0.0.1:8000/api/voice/clone" \
  -F "name=My Voice" \
  -F "files=@sample.mp3"
```

**Response**:
```json
{
  "voice_id": "c38kUX8pkfYO2kHyqfFy",
  "requires_verification": false
}
```

Use `voice_id` for text-to-speech or store it per user for “Re-record My Voice” in the Profile screen.

## Deepen Manifestation

**POST** `/api/stories/deepen`

Generates a sensory-rich continuation (deepening) of an existing story using the DEEPEN_MANIFESTATION prompt: first-person past tense, 300–500 words, specific locations and sensory details, ending with "Already done."

- **Body**: `user_id`, `story_id`, `name`, `location`, `energyWord`, optional `lovedOne`
- **Subscription**: Same as story generate (free: 1 story per day; monthly/annual: unlimited)
- **Database**: The `Stories` table must have `parent_story_id` (integer, nullable) and `deepening_level` (integer). Add them if missing:

  ```sql
  ALTER TABLE "Stories" ADD COLUMN IF NOT EXISTS parent_story_id integer REFERENCES "Stories"(id);
  ALTER TABLE "Stories" ADD COLUMN IF NOT EXISTS deepening_level integer DEFAULT 0;
  ```
