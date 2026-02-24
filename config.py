# prompt_service/config.py

PROMPT_SERVICE_PORT = 8001
PROMPT_SERVICE_HOST = "0.0.0.0"

# Nami connection (we deliver interjections here)
NAMI_INTERJECT_URL = "http://localhost:8000/funnel/interject"

# Director Engine (we fetch context from here before forwarding to Nami)
DIRECTOR_URL = "http://localhost:8006"

# Speech timing
POST_SPEECH_COOLDOWN = 5.0       # Breather after Nami finishes speaking
SPEECH_TIMEOUT = 60.0            # Failsafe: force-unlock after this
MIN_SPEECH_INTERVAL = 5.0        # Min gap between dispatches
POST_RESPONSE_COOLDOWN = 10.0    # Cooldown after Nami responds to user

# Context fetch timeout — if director doesn't respond in time, fall back to
# sending req.content alone so Nami still fires rather than silently failing
DIRECTOR_CONTEXT_TIMEOUT = 3.0