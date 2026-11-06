from dotenv import load_dotenv
from prompt import AGENT_INSTRUCTION, AGENT_RESPONSE

from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import google, noise_cancellation

import asyncio

load_dotenv()


class Assistant(Agent):
    def __init__(self):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self.last_active = asyncio.get_event_loop().time()
        self.awaiting_wake = False
        self.memory = {}

    async def on_session_start(self, session: AgentSession):
        asyncio.create_task(self.monitor_idle(session))

    def detect_emotion(self, message: str) -> str:
        message = message.lower()
        if any(word in message for word in ["emergency", "urgent", "problem", "crash", "now"]):
            return "urgent"
        if any(word in message for word in ["thanks", "awesome", "good", "great", "cool"]):
            return "friendly"
        if any(word in message for word in ["tired", "i'm sad", "stress", "depressed", "lonely"]):
            return "supportive"
        if any(word in message for word in ["shut down", "deploy", "initiate", "do it now"]):
            return "tactical"
        return "neutral"

    async def on_message(self, ctx: AgentSession, message: str) -> str:
        now = asyncio.get_event_loop().time()
        message = message.strip().lower()

        if "sleep now" in message:
            self.awaiting_wake = True
            print("😴 FRIDAY is going to sleep on command.")
            await ctx.speak("Going idle, Boss.")
            return ""

        if self.awaiting_wake:
            if "friday" in message:
                self.awaiting_wake = False
                self.last_active = now
                await ctx.speak("I'm here, Boss.")
                return ""
            return ""

        if ctx and ctx.is_speaking:
            await ctx.stop_speaking()

        self.last_active = now

        # 🎭 Emotion detection and styling
        emotion = self.detect_emotion(message)

        emotion_style = {
            "urgent": "Respond urgently and seriously, like a tactical alert.",
            "friendly": "Use a cheerful, upbeat, and relaxed tone.",
            "supportive": "Respond gently and calmly with emotional support.",
            "tactical": "Be concise and direct, like a mission operator.",
            "neutral": "Maintain a clear, respectful, and professional tone."
        }

        tone_instruction = emotion_style.get(emotion, emotion_style["neutral"])

        styled_prompt = f"{AGENT_RESPONSE.replace('{{user_query}}', message)}\n\n{tone_instruction}"

        return styled_prompt

    async def monitor_idle(self, session: AgentSession):
        while True:
            await asyncio.sleep(5)
            if self.awaiting_wake:
                continue
            now = asyncio.get_event_loop().time()
            if now - self.last_active > 60:
                self.awaiting_wake = True
                print("💤 FRIDAY is now idle. Say 'Friday' to wake her.")
                # await session.speak("I'm going idle. Say 'Friday' to wake me.")


async def entrypoint(ctx: agents.JobContext):
    assistant = Assistant()

    session = AgentSession(
        llm=google.beta.realtime.RealtimeModel(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            # Changed to 'Aoede' - a female voice name known to be compatible with Gemini Live API.
            voice="Aoede",
            temperature=0.8,
            instructions=AGENT_INSTRUCTION,
          #  video_enabled=True,
        ),
    )

    await session.start(
        room=ctx.room,
        agent=assistant,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))



