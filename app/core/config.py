from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from environment."""

    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_BASE_URL: str = "https://api.elevenlabs.io"
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "Record-Stories"
    # Claude API
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_STORY_MODEL: str = "claude-sonnet-4-20250514"
    CLAUDE_STORY_MAX_TOKENS: int = 900  # enough for 2600 chars + theme

    # Stripe (subscription paywall: 7-day trial, monthly and annual plans)
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_ID_ANNUAL: str = ""
    STRIPE_PRICE_ID_MONTHLY: str = ""
    STRIPE_TRIAL_DAYS: int = 7
    # Optional override. If empty, app uses client SYSTEM_PROMPT from story_prompts (first-person past tense, 3rd grade vocab).
    STORY_SYSTEM_PROMPT: str = ""
    # FCM: path to Firebase service account JSON (for reminder push notifications). Empty = reminders not sent.
    FIREBASE_CREDENTIALS_PATH: str = ""
    # Auth: JWT for login/signup. Override in .env for production (e.g. openssl rand -hex 32).
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    # RevenueCat webhook: exact value expected in Authorization header (set in .env).
    REVENUECAT_WEBHOOK_AUTHORIZATION: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

# Story length and Claude instructions (used by app.core.claude)
STORY_MIN_CHARS = 1000
STORY_MAX_CHARS = 2600


# Onboarding inputs for story generation (from Already Done flow)
CATEGORIES = ("Love", "Money", "Career", "Health", "Home")
ENERGY_WORDS = ("Powerful", "Peaceful", "Abundant", "Grateful", "Confident")

THEME_BY_CATEGORY = {
    "Love": "A Love That Was Already Yours",
    "Money": "The Abundance That Arrived",
    "Career": "The Career That Was Already Yours",
    "Health": "The Vitality That Was Already Yours",
    "Home": "The Home That Was Already Yours",
}

DESCRIBE_ENGINE_INSTRUCTION = """

**PRIMARY ENGINE — "Describe what's already theirs" (user's words):**
This is the most important input. The story AND the theme MUST be driven directly by this text. Do not substitute a generic or beautiful narrative. Every core idea, feeling, and detail in the story must come from what the user wrote. If their words are short, vague, or unusual, the story must still reflect and expand only from those words — never invent a different desire. The theme must also reflect this same specific desire, not a generic category headline."""

OUTPUT_FORMAT_INSTRUCTION = f"""

**Output format (follow exactly):**
1. First line: THEME: <your theme>
   Theme style: "A Love That Was Already Yours", "The Love You'd Always Known", "The Abundance That Arrived" — short, evocative. The theme MUST reflect the user's specific desire, not a generic category.
2. One blank line.
3. Then the story only (no headers). Aim for 350–450 words; do not exceed {STORY_MAX_CHARS} characters."""
