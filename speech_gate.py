# prompt_service/speech_gate.py
"""
The Gate. Controls when Nami can and cannot speak.

All speaking state lives here — the brain no longer tracks any of this.
The brain fires requests freely; we decide what actually reaches Nami.

Gates (in order):
1. Is Nami currently speaking? (block)
2. Post-speech cooldown / breather? (block)
3. Min interval between dispatches? (block)
4. Post-response cooldown? (block)
5. Already reacted to this event? (block)
6. All clear → forward to Nami
"""

import time
from typing import Optional, Dict, Any


class SpeechGate:
    def __init__(self, config):
        # --- SPEECH STATE ---
        self.nami_is_speaking: bool = False
        self.speech_started_time: float = 0.0
        self.last_speech_finished_time: float = 0.0
        self.last_speech_source: Optional[str] = None
        self.awaiting_user_response: bool = False

        # --- INTERRUPT TRACKING ---
        self.last_interrupt_time: float = 0.0
        self.interrupt_count: int = 0

        # --- DISPATCH TRACKING ---
        self.last_dispatch_time: float = 0.0
        self.last_user_response_time: float = 0.0

        # --- DEDUP ---
        self.reacted_event_ids: set = set()
        self.max_tracked_events: int = 50

        # --- CONFIG ---
        self.speech_timeout = config.SPEECH_TIMEOUT
        self.post_speech_cooldown = config.POST_SPEECH_COOLDOWN
        self.min_speech_interval = config.MIN_SPEECH_INTERVAL
        self.post_response_cooldown = config.POST_RESPONSE_COOLDOWN

    # =================================================================
    # SPEECH STATE (Nami's TTS reports here)
    # =================================================================

    def set_speaking(self, is_speaking: bool, source: str = None):
        """Called when Nami's TTS starts or stops."""
        self.nami_is_speaking = is_speaking

        if is_speaking:
            self.speech_started_time = time.time()
            if source:
                self.last_speech_source = source
            print(f"🔇 [Gate] Nami started speaking (source: {source})")
        else:
            duration = time.time() - self.speech_started_time if self.speech_started_time else 0
            self.last_speech_finished_time = time.time()
            print(
                f"🔊 [Gate] Nami finished ({duration:.1f}s, was: {self.last_speech_source}) "
                f"- {self.post_speech_cooldown}s breather"
            )
            # Clear awaiting — she just replied, breather handles the gap
            if self.awaiting_user_response:
                self.awaiting_user_response = False
                print(f"✅ [Gate] Awaiting cleared — replied to user")

    def is_speaking(self) -> bool:
        """Check if Nami is currently speaking (with timeout failsafe)."""
        if not self.nami_is_speaking:
            return False
        # Timeout failsafe
        if self.speech_started_time and (time.time() - self.speech_started_time) > self.speech_timeout:
            print(f"⚠️ [Gate] Timeout ({self.speech_timeout}s) - forcing unlock")
            self.nami_is_speaking = False
            self.awaiting_user_response = False
            return False
        return True

    def in_cooldown(self) -> bool:
        """Brief breather after Nami finishes speaking."""
        if self.last_speech_finished_time == 0.0:
            return False
        return (time.time() - self.last_speech_finished_time) < self.post_speech_cooldown

    # =================================================================
    # GATING LOGIC
    # =================================================================

    def can_speak(self) -> Dict[str, Any]:
        """
        Run all gates. Returns {"allowed": bool, "reason": str}.
        
        This is the core function — every speech request goes through here.
        """
        # Gate 1: Never interrupt herself
        if self.is_speaking():
            return {"allowed": False, "reason": "nami_speaking"}

        # Gate 2: Post-speech breather (no machine-gun)
        if self.in_cooldown():
            return {"allowed": False, "reason": "post_speech_cooldown"}

        now = time.time()

        # Gate 3: Min interval since last dispatch
        if (now - self.last_dispatch_time) < self.min_speech_interval:
            return {"allowed": False, "reason": "min_interval"}

        # Gate 4: Post-response cooldown (don't yap right after answering user)
        if (now - self.last_user_response_time) < self.post_response_cooldown:
            return {"allowed": False, "reason": "post_response_cooldown"}

        return {"allowed": True, "reason": "ok"}

    def check_event_reacted(self, event_id: Optional[str]) -> bool:
        """Returns True if we already reacted to this event."""
        if not event_id:
            return False
        return event_id in self.reacted_event_ids

    # =================================================================
    # REGISTRATION (Record that something happened)
    # =================================================================

    def register_dispatch(self, event_id: str = None):
        """Record that we dispatched a speech request."""
        self.last_dispatch_time = time.time()
        if event_id:
            self.reacted_event_ids.add(event_id)
            if len(self.reacted_event_ids) > self.max_tracked_events:
                self.reacted_event_ids.pop()

    def register_user_response(self):
        """Brain signals: Nami just responded to a user interaction."""
        self.last_user_response_time = time.time()
        print(f"🎯 [Gate] User response registered - cooldown active for {self.post_response_cooldown}s")

    def clear_user_awaiting(self):
        """Brain signals: user spoke again, stop waiting."""
        if self.awaiting_user_response:
            print(f"✅ [Gate] User responded - awaiting cleared")
        self.awaiting_user_response = False

    # =================================================================
    # INTERRUPT
    # =================================================================

    def interrupt(self, reason: str = "direct_mention") -> bool:
        """
        Force-interrupt Nami's current speech.
        
        Returns True if she was actually speaking (and got interrupted).
        
        After interrupt:
        - Speaking lock cleared so the interrupt interjection can be sent
        - awaiting_user_response set True so idle/proactive stays suppressed
          until the user speaks again naturally
        """
        was_speaking = self.nami_is_speaking
        self.nami_is_speaking = False
        self.last_interrupt_time = time.time()
        self.interrupt_count += 1
        self.awaiting_user_response = True

        if was_speaking:
            print(f"🛑 [Gate] INTERRUPT! Reason: {reason}")
        else:
            print(f"🛑 [Gate] Interrupt requested but Nami wasn't speaking (reason: {reason})")

        return was_speaking

    # =================================================================
    # DEBUG / STATUS
    # =================================================================

    def get_stats(self) -> Dict[str, Any]:
        return {
            "nami_speaking": self.is_speaking(),
            "awaiting_user_response": self.awaiting_user_response,
            "in_cooldown": self.in_cooldown(),
            "total_interrupts": self.interrupt_count,
            "last_interrupt_time": self.last_interrupt_time,
            "seconds_since_interrupt": (
                round(time.time() - self.last_interrupt_time, 1)
                if self.last_interrupt_time else None
            ),
            "last_dispatch_time": self.last_dispatch_time,
            "speech_source": self.last_speech_source,
        }