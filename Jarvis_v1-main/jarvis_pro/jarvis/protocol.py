"""Names shared by the dashboard server and the voice agent: LiveKit topics, RPCs and participant
attributes. No dependencies, so server.py can use them without loading the voice-agent stack."""

EVENTS_TOPIC = "jarvis.events"
APPROVE_RPC = "jarvis.approve"
OWNER_ROLE_ATTR = "jarvis.role"  # "owner" | "guest"
WAKE_ATTR = "jarvis.wake"  # first command spoken with the wake word ("Hey JARVIS, ...")
DEVICE_ATTR = "jarvis.device"  # "host" (the PC running JARVIS), "remote" (browser elsewhere), "app" (Device app)
DEVICE_ID_ATTR = "jarvis.device_id"
DEVICE_NAME_ATTR = "jarvis.device_name"
