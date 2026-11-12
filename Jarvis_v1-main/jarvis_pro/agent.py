"""JARVIS PRO — voice agent entrypoint.

    python agent.py console   # talk in the terminal (mic + speakers)
    python agent.py dev       # serve the dashboard / phone calls via LiveKit

One worker handles these kinds of sessions:
  0. JARVIS Device app    -> OwnerAgent or GuestAgent; device tools run on THAT device only
  1. Dashboard / console  -> OwnerAgent (full access, per access.yaml)
  2. Inbound phone call   -> CallerAgent (take message / transfer / hang up)
                             or OwnerAgent if the number is a trusted owner number
  3. Outbound phone call  -> CallerAgent with a purpose (dispatched by make_phone_call)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from google.genai import types as genai_types  # noqa: E402
from livekit import api, rtc  # noqa: E402
from livekit.agents import AgentServer, AgentSession, JobContext, cli, room_io  # noqa: E402
from livekit.plugins import google, noise_cancellation  # noqa: E402

from jarvis import audit, config  # noqa: E402
from jarvis.access import DENY, get_policy  # noqa: E402
from jarvis import device_link  # noqa: E402
from jarvis.agents import CallerAgent, GuestAgent, OwnerAgent  # noqa: E402
from jarvis.guard import (  # noqa: E402
    DEVICE_ATTR, DEVICE_ID_ATTR, DEVICE_NAME_ATTR, OWNER_ROLE_ATTR, SessionData,
)
from jarvis.mcp_client import build_toolsets  # noqa: E402

logger = logging.getLogger("jarvis")

server = AgentServer()


def realtime_model() -> google.realtime.RealtimeModel:
    extra = {}
    if "2.5" in config.REALTIME_MODEL and config.THINKING_BUDGET >= 0:
        # Native-audio 2.5 models think before every reply unless told not to.
        extra["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=config.THINKING_BUDGET)
    return google.realtime.RealtimeModel(
        model=config.REALTIME_MODEL,
        # google-genai 3.x defaults to API "v1", where the Live endpoint returns 404.
        api_version="v1beta",
        voice=config.VOICE,
        temperature=config.TEMPERATURE,
        # Decide faster that you've stopped talking (default waits noticeably longer).
        realtime_input_config=genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=config.SILENCE_MS,
                prefix_padding_ms=100,
            )
        ),
        **extra,
    )


@server.rtc_session(agent_name=config.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    policy = get_policy()
    meta = json.loads(ctx.job.metadata or "{}") if ctx.job.metadata else {}

    if meta.get("mode") == "outbound":
        await _outbound_call(ctx, meta)
        return

    if ctx.is_fake_job():  # console mode: you are the owner, no participant to wait for
        await _start_owner(ctx, participant=None, video=False)
        return

    await ctx.connect()
    participant = await ctx.wait_for_participant()

    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        number = participant.attributes.get("sip.phoneNumber")
        phone = policy.section("phone")
        trusted = phone.get("trust_caller_id") is True and number in (phone.get("owner_numbers") or [])
        audit.record({"capability": "phone.inbound", "summary": f"Incoming call from {number}",
                      "status": "owner" if trusted else "caller"})
        if trusted:
            await _start_owner(ctx, participant, video=False, telephony=True)
        else:
            await _start_caller(ctx, participant, number=number)
        return

    if participant.attributes.get(DEVICE_ATTR) == "app":
        await _start_device(ctx, participant)
        return

    await _start_owner(ctx, participant, video=policy.level("screen.live_share") != DENY)


async def _start_device(ctx: JobContext, participant: rtc.RemoteParticipant) -> None:
    """A JARVIS Device app session. Each device has its own room and its own session, so
    a command from one device is only ever carried out on that same device."""
    policy = get_policy()
    # Attributes are set by server.py in the signed token; the device can't change them.
    role = participant.attributes.get(OWNER_ROLE_ATTR)
    name = participant.attributes.get(DEVICE_NAME_ATTR) or "this device"
    if role not in ("owner", "guest") or not participant.attributes.get(DEVICE_ID_ATTR):
        logger.warning("device %s has no valid role/id; refusing", participant.identity)
        ctx.shutdown()
        return
    device_link.install(ctx.room)
    audit.record({"capability": "device.connect", "summary": f"{name} connected ({role})", "status": role})
    session = AgentSession(
        llm=realtime_model(),
        userdata=SessionData(mode=role, device_identity=participant.identity, device_name=name,
                             device_id=participant.attributes.get(DEVICE_ID_ATTR)),
    )
    agent = (OwnerAgent(policy, toolsets=build_toolsets(policy), device_name=name) if role == "owner"
             else GuestAgent(policy, device_name=name))
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=participant.identity,
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVC()),
            delete_room_on_close=True,
        ),
    )


async def _start_owner(ctx: JobContext, participant: rtc.RemoteParticipant | None, *,
                       video: bool, telephony: bool = False) -> None:
    policy = get_policy()
    session = AgentSession(llm=realtime_model(), userdata=SessionData(mode="owner"))
    nc = noise_cancellation.BVCTelephony() if telephony else noise_cancellation.BVC()
    options = room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(noise_cancellation=nc),
        video_input=video,  # sees the dashboard's screen share / camera when allowed
        **({"participant_identity": participant.identity} if participant else {}),
    )
    remote = bool(participant and participant.attributes.get(DEVICE_ATTR) == "remote")
    agent = OwnerAgent(policy, toolsets=build_toolsets(policy), remote=remote)
    await session.start(agent=agent, room=ctx.room, room_options=options)


async def _start_caller(ctx: JobContext, participant: rtc.RemoteParticipant, *, number: str | None) -> None:
    policy = get_policy()
    session = AgentSession(
        llm=realtime_model(),
        userdata=SessionData(mode="caller", caller_number=number, caller_identity=participant.identity),
    )
    await session.start(
        agent=CallerAgent(policy, number=number),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=participant.identity,
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
            delete_room_on_close=True,
        ),
    )


async def _outbound_call(ctx: JobContext, meta: dict) -> None:
    policy = get_policy()
    number, purpose = meta["phone_number"], meta.get("purpose", "")
    if policy.level("phone.call_out") == DENY:  # policy may have changed since dispatch
        logger.warning("outbound call to %s blocked by access.yaml", number)
        ctx.shutdown()
        return

    await ctx.connect()
    session = AgentSession(
        llm=realtime_model(),
        userdata=SessionData(mode="caller", caller_number=number, caller_identity=number, purpose=purpose),
    )
    await session.start(
        agent=CallerAgent(policy, number=number, purpose=purpose),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=number,
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
            delete_room_on_close=True,
        ),
    )

    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=config.env("SIP_OUTBOUND_TRUNK_ID") or "",
                sip_call_to=number,
                participant_identity=number,
                wait_until_answered=True,
            )
        )
    except api.TwirpError as e:
        audit.record({"capability": "phone.call_out", "summary": f"Call to {number} failed",
                      "status": "failed", "error": f"{e.message} (SIP {e.metadata.get('sip_status_code')})"})
        ctx.shutdown()
        return

    audit.record({"capability": "phone.call_out", "summary": f"Call to {number} answered", "status": "connected"})
    session.generate_reply(instructions="The person just answered. Introduce yourself and state the purpose.")


if __name__ == "__main__":
    cli.run_app(server)
