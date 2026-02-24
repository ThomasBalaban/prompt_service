# prompt_service/main.py
"""
Nami Prompt Service — The Mouth.

Sits between the Director Engine (brain) and Nami (LLM + TTS).

Responsibilities:
  - Gates all speech requests (cooldowns, dedup, interrupt handling)
  - Fetches full structured context from the Director before forwarding
  - Forwards approved interjections to Nami
  - Tracks Nami's speaking state (TTS reports here)

The brain fires requests freely. We decide what reaches Nami,
and we enrich what we send with the full director context block.

Port: 8001
Director (brain): 8006
Nami:  8000
"""

import uvicorn
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Dict, Any, Optional

import config
from speech_gate import SpeechGate

# =====================================================
# APP SETUP
# =====================================================
app = FastAPI(title="Nami Prompt Service")
gate = SpeechGate(config)
http_client: Optional[httpx.AsyncClient] = None


# =====================================================
# MODELS
# =====================================================

class SpeakRequest(BaseModel):
    """Inbound from the brain."""
    trigger: str           # "skill_issue", "thought", "reactive", "interrupt", "dead_air", etc.
    content: str           # The interjection trigger / instruction for Nami
    priority: float = 0.5  # 0.0 = highest, 1.0 = lowest
    source: str = "DIRECTOR"
    is_interrupt: bool = False
    event_id: Optional[str] = None
    metadata: Dict[str, Any] = {}


class SpeechStatePayload(BaseModel):
    """Inbound from Nami's TTS."""
    source: Optional[str] = None


# =====================================================
# ENDPOINTS — Brain → Prompt Service
# =====================================================

@app.post("/speak")
async def handle_speak(req: SpeakRequest):
    """
    The brain pushes ALL speech requests here.
    We gate them and forward survivors to Nami — with full context attached.
    """
    # --- INTERRUPT PATH (bypasses normal gates) ---
    if req.is_interrupt:
        was_speaking = gate.interrupt(reason=req.trigger)

        # If she was speaking, signal Nami's TTS to stop
        if was_speaking:
            await _signal_nami_interrupt(req.trigger)

        # Forward the interrupt interjection (with context)
        success = await _forward_to_nami(req)
        return {
            "delivered": success,
            "interrupted": was_speaking,
            "gate_result": "interrupt_bypass",
        }

    # --- NORMAL PATH (full gate check) ---

    # Dedup check
    if gate.check_event_reacted(req.event_id):
        return {"delivered": False, "gate_result": "already_reacted"}

    # Run gates
    check = gate.can_speak()
    if not check["allowed"]:
        print(f"🚫 [Gate] Blocked: {check['reason']} | {req.trigger}: {req.content[:40]}...")
        return {"delivered": False, "gate_result": check["reason"]}

    # All gates passed — enrich with context and forward to Nami
    success = await _forward_to_nami(req)

    if success:
        gate.register_dispatch(event_id=req.event_id)

    return {
        "delivered": success,
        "gate_result": "ok" if success else "nami_rejected",
    }


@app.post("/user_responded")
async def user_responded():
    """Brain signals: user spoke (direct mic / mention). Clears awaiting state."""
    gate.clear_user_awaiting()
    return {"status": "ok"}


@app.post("/register_bot_response")
async def register_bot_response():
    """Brain signals: Nami just responded to a user interaction. Start cooldown."""
    gate.register_user_response()
    return {"status": "ok"}


# =====================================================
# ENDPOINTS — Nami → Prompt Service
# =====================================================

@app.post("/speech_started")
async def speech_started(payload: SpeechStatePayload = SpeechStatePayload()):
    """Nami's TTS reports: started speaking."""
    gate.set_speaking(True, source=payload.source)
    return {"status": "ok"}


@app.post("/speech_finished")
async def speech_finished():
    """Nami's TTS reports: done speaking."""
    gate.set_speaking(False)
    return {"status": "ok"}


# =====================================================
# ENDPOINTS — Status / Debug
# =====================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "prompt_service"}


@app.get("/gate_status")
async def gate_status():
    """Full gate state for debugging."""
    check = gate.can_speak()
    return {
        **gate.get_stats(),
        "can_speak": check["allowed"],
        "block_reason": check["reason"] if not check["allowed"] else None,
    }


@app.get("/speech_state")
async def speech_state():
    """Quick check: is Nami speaking?"""
    return {"is_speaking": gate.is_speaking()}


# =====================================================
# INTERNAL — Context Fetch from Director
# =====================================================

async def _fetch_director_context(req: SpeakRequest) -> Optional[Dict[str, Any]]:
    """
    Call the Director's /context endpoint to get the full structured prompt.

    Passes the trigger and metadata back so the director can tailor the
    context to the specific speech event (e.g. a skill issue vs dead air
    may result in different memory retrieval or detail levels).

    Returns the full context dict, or None if the director is unreachable
    or times out — caller falls back to sending trigger-only content so
    Nami still fires rather than silently dropping the request.
    """
    global http_client
    if not http_client:
        return None

    payload = {
        "trigger": req.trigger,
        "event_id": req.event_id,
        "metadata": req.metadata,
    }

    try:
        response = await http_client.post(
            f"{config.DIRECTOR_URL}/context",
            json=payload,
            timeout=config.DIRECTOR_CONTEXT_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
            print(
                f"📋 [Prompt] Got context from Director "
                f"({len(data.get('context', ''))} chars, "
                f"scene={data.get('scene', '?')}, "
                f"mood={data.get('mood', '?')})"
            )
            return data
        else:
            print(f"⚠️ [Prompt] Director /context returned {response.status_code}")
            return None
    except httpx.ConnectError:
        print(f"⚠️ [Prompt] Director unreachable at {config.DIRECTOR_URL} — sending without context")
        return None
    except httpx.TimeoutException:
        print(f"⚠️ [Prompt] Director /context timed out ({config.DIRECTOR_CONTEXT_TIMEOUT}s) — sending without context")
        return None
    except Exception as e:
        print(f"⚠️ [Prompt] Context fetch error: {e}")
        return None


# =====================================================
# INTERNAL — Delivery to Nami
# =====================================================

async def _forward_to_nami(req: SpeakRequest) -> bool:
    """
    Fetch full context from the Director, then forward the enriched
    interjection to Nami's funnel.

    What Nami receives:
      - context:  The full structured prompt block from the director
                  (visual summary, event log, memories, directive, etc.)
      - content:  The specific trigger / instruction from the brain
                  (e.g. "Skill Issue Detected", "Dead Air", or the user's
                  actual words for a direct address)

    If the director is down we fall back to sending content alone so
    Nami still fires rather than silently dropping the request.
    """
    global http_client
    if not http_client:
        print("❌ [Prompt] No HTTP client!")
        return False

    # --- Fetch context from Director ---
    director_data = await _fetch_director_context(req)
    context_block = director_data.get("context", "") if director_data else ""

    if not context_block:
        print(f"⚠️ [Prompt] No context block — Nami will use base personality for: {req.trigger}")

    # --- Build payload for Nami ---
    payload = {
        # Full structured context (visual, audio, chat, memories, directive)
        "context": context_block,
        # The specific trigger instruction the brain generated
        "content": req.content,
        "priority": 0.0 if req.is_interrupt else req.priority,
        "source_info": {
            "source": req.source,
            "use_tts": True,
            "is_interrupt": req.is_interrupt,
            # Pass through director state so Nami's prompt builder can use it
            "mood": director_data.get("mood") if director_data else None,
            "scene": director_data.get("scene") if director_data else None,
            "directive": director_data.get("directive") if director_data else None,
            "active_user": director_data.get("active_user") if director_data else None,
            **req.metadata,
        },
    }

    try:
        response = await http_client.post(
            config.NAMI_INTERJECT_URL,
            json=payload,
            timeout=2.0,
        )
        if response.status_code == 200:
            print(f"✅ [Prompt] Delivered: {req.trigger} → {req.content[:50]}...")
            return True
        else:
            print(f"❌ [Prompt] Nami rejected: {response.status_code}")
            return False
    except httpx.ConnectError:
        print(f"❌ [Prompt] Cannot reach Nami at {config.NAMI_INTERJECT_URL}")
        return False
    except Exception as e:
        print(f"❌ [Prompt] Delivery error: {e}")
        return False


async def _signal_nami_interrupt(reason: str):
    """
    Tell Nami's TTS to stop immediately.
    This kills the current audio playback via sd.stop() so the
    interrupt interjection can be heard without waiting.
    """
    global http_client
    if not http_client:
        return

    try:
        response = await http_client.post(
            "http://localhost:8000/stop_audio",
            timeout=1.0,
        )
        if response.status_code == 200:
            print(f"🛑 [Prompt] Audio killed on Nami (reason: {reason})")
        else:
            print(f"⚠️ [Prompt] Stop audio returned {response.status_code}")
    except Exception as e:
        print(f"⚠️ [Prompt] Failed to stop Nami audio: {e}")


# =====================================================
# LIFECYCLE
# =====================================================

@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient()
    print(f"🎤 Prompt Service ready on port {config.PROMPT_SERVICE_PORT}")
    print(f"   → Director: {config.DIRECTOR_URL}/context")
    print(f"   → Nami:     {config.NAMI_INTERJECT_URL}")


@app.on_event("shutdown")
async def shutdown():
    global http_client
    if http_client:
        await http_client.aclose()
        http_client = None
    print("🛑 Prompt Service shut down")


# =====================================================
# ENTRYPOINT
# =====================================================

if __name__ == "__main__":
    print("🎤 PROMPT SERVICE (The Mouth) - Starting...")
    uvicorn.run(
        app,
        host=config.PROMPT_SERVICE_HOST,
        port=config.PROMPT_SERVICE_PORT,
        log_level="warning",
    )