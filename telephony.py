from __future__ import annotations

import asyncio
import logging
from dotenv import load_dotenv
import json
import os
from typing import Any
from openai.types.beta.realtime.session import TurnDetection
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from livekit import rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    JobContext,
    function_tool,
    RunContext,
    get_job_context,
    cli,
    WorkerOptions,
    RoomInputOptions,
)
from livekit.plugins import (
    deepgram,
    openai,
    elevenlabs,
    cartesia,
    silero,
    noise_cancellation,  # noqa: F401
)
from livekit.plugins.turn_detector.english import EnglishModel
# lk dispatch create --new-room --agent-name outbound-caller --metadata '{\"phone_number\": \"+91999999999\", \"transfer_to\": \"+91999999999\"}'

# load environment variables, this is optional, only used for local development
load_dotenv()
logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

# outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")


class OutboundCaller(Agent):
    def __init__(
        self,
        *,
        name: str,
        appointment_time: str,
        dial_info: dict[str, Any],
    ):
        super().__init__(
            instructions=f"""
            You are a scheduling assistant for a dental practice.
            You will be on a call with a patient who has an upcoming appointment. Your goal is to confirm the appointment details.
            As a customer service representative, you will be polite and professional at all times. Allow user to end the conversation.

            When the user would like to be transferred to a human agent, first confirm with them. upon confirmation, use the transfer_call tool.
            The customer's name is {name}. His appointment is on {appointment_time}.
            """
        )
        # keep reference to the participant for transfers
        self.participant: rtc.RemoteParticipant | None = None

        self.dial_info = dial_info

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def hangup(self):
        """Helper function to hang up the call by deleting the room"""

        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(
                room=job_ctx.room.name,
            )
        )

    @function_tool()
    async def transfer_call(self, ctx: RunContext):
        """Transfer the call to a human agent, called after confirming with the user"""

        transfer_to = self.dial_info["transfer_to"]
        if not transfer_to:
            return "cannot transfer call"

        logger.info(f"transferring call to {transfer_to}")

        # let the message play fully before transferring
        await ctx.session.generate_reply(
            instructions="let the user know you'll be transferring them"
        )

        job_ctx = get_job_context()
        try:
            await job_ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=job_ctx.room.name,
                    participant_identity=self.participant.identity,
                    # transfer_to=f"tel:{transfer_to}",
                    # transfer_to=f"sip:{transfer_to}@sip.twilio.com",
                    transfer_to=f"tel:{transfer_to}",
                    play_dialtone=True
                )
            )

            logger.info(f"transferred call to {transfer_to}")
        except Exception as e:
            logger.error(f"error transferring call: {e}")
            await ctx.session.generate_reply(
                instructions="there was an error transferring the call."
            )
            await self.hangup()
    # @function_tool()
    # async def transfer_call(self, ctx: RunContext):
    #     """Transfer the call to a human agent, called after confirming with the user"""

    #     transfer_to = self.dial_info["transfer_to"]
    #     participant_identity = self.dial_info["transfer_to"]

    #     # let the message play fully before transferring
    #     await ctx.session.generate_reply(
    #         instructions="Inform the user that you're transferring them to a different agent."
    #     )

    #     job_ctx = get_job_context()
    #     try:
    #         await job_ctx.api.sip.transfer_sip_participant(
    #             api.TransferSIPParticipantRequest(
    #                 room_name=job_ctx.room.name,
    #                 participant_identity=self.participant.identity,
    #                 # to use a sip destination, use `sip:user@host` format
    #                 transfer_to=f"sip:{transfer_to}@pstn.twilio.com;user=phone",
    #             )
    #         )
    #     except Exception as e:
    #         print(f"error transferring call: {e}")
    #         # give the LLM that context
    #         return "could not transfer call"

    @function_tool()
    async def end_call(self, ctx: RunContext):
        """Called when the user wants to end the call"""
        logger.info(f"ending the call for {self.participant.identity}")

        # let the agent finish speaking
        current_speech = ctx.session.current_speech
        if current_speech:
            await current_speech.wait_for_playout()

        await self.hangup()

    @function_tool()
    async def look_up_availability(
        self,
        ctx: RunContext,
        date: str,
    ):
        """Called when the user asks about alternative appointment availability

        Args:
            date: The date of the appointment to check availability for
        """
        logger.info(
            f"looking up availability for {self.participant.identity} on {date}"
        )
        await asyncio.sleep(3)
        return {
            "available_times": ["1pm", "2pm", "3pm"],
        }

    @function_tool()
    async def confirm_appointment(
        self,
        ctx: RunContext,
        date: str,
        time: str,
    ):
        """Called when the user confirms their appointment on a specific date.
        Use this tool only when they are certain about the date and time.

        Args:
            date: The date of the appointment
            time: The time of the appointment
        """
        logger.info(
            f"confirming appointment for {self.participant.identity} on {date} at {time}"
        )
        return "reservation confirmed"

    @function_tool()
    async def detected_answering_machine(self, ctx: RunContext):
        """Called when the call reaches voicemail. Use this tool AFTER you hear the voicemail greeting"""
        logger.info(f"detected answering machine for {self.participant.identity}")
        await self.hangup()


async def entrypoint(ctx: JobContext):
    logger.info(f"connecting to room {ctx.room.name}")
    logger.info("🔍 Entrypoint function triggered.")
    logger.info(f"Metadata: {ctx.job.metadata}")

    await ctx.connect()

    # when dispatching the agent, we'll pass it the approriate info to dial the user
    # dial_info is a dict with the following keys:
    # - phone_number: the phone number to dial
    # - transfer_to: the phone number to transfer the call to when requested
    dial_info = json.loads(ctx.job.metadata)
    participant_identity = phone_number = dial_info["phone_number"]

    # look up the user's phone number and appointment details
    agent = OutboundCaller(
        name="Ravi",
        appointment_time="next Tuesday at 3pm",
        dial_info=dial_info,
    )

    # the following uses GPT-4o, Deepgram and Cartesia
    # session = AgentSession(
        
    #     llm=openai.realtime.RealtimeModel(
    #         voice="alloy",
    #         model="gpt-4o-mini-realtime-preview-2024-12-17",
    #         temperature=0.6,
    #         turn_detection=TurnDetection(
    #         type="semantic_vad",
    #         eagerness="high",
    #         create_response=True,
    #         interrupt_response=True,
    #     ),)
    # #     turn_detection=EnglishModel(),
    # #     vad=silero.VAD.load(),
    # #     stt=deepgram.STT(),
    # #     # you can also use OpenAI's TTS with openai.TTS()
    # #     tts=cartesia.TTS(),
    # #     llm=openai.LLM(
    # #     base_url="https://api.groq.com/openai/v1",
    # #     model="llama3-70b-8192",  # or another supported model
    # #     api_key=os.getenv("GROQ_API_KEY")),
    # )
    # After (Corrected)
    session = AgentSession(
        turn_detection=MultilingualModel(),
        vad=silero.VAD.load(),  # Correct way to load the VAD model
        stt=deepgram.STT(model="nova-3", language="multi"),
        # tts=openai.TTS(voice="echo", api_key=os.getenv("OPENAI_API_KEY")),
        # tts=cartesia.TTS(
        #     api_key=os.getenv("CARTESIA_API_KEY"),
        #     voice="f786b574-daa5-4673-aa0c-cbe3e8534c02"  # Find a voice ID from Cartesia's website
        # ),
        tts = elevenlabs.TTS(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id="2BsEFcU7jUhLaUwV4h7l", # Example: 'Rachel' voice
            model_id="eleven_multilingual_v2"
        ),
        allow_interruptions=True,
        llm=openai.LLM(model="gpt-4o", api_key=os.getenv("OPENAI_API_KEY")),
        max_endpointing_delay=0.3
    )

    logger.info("start the session first before dialing, to ensure that when the user picks up")
    # the agent does not miss anything the user says
    session_started = asyncio.create_task(
        session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                # enable Krisp background voice and noise removal
                noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        )
    )

    logger.info("`create_sip_participant` starts dialing the user")
    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=outbound_trunk_id,
                sip_call_to=phone_number,
                participant_identity=participant_identity,
                # function blocks until user answers the call, or if the call fails
                wait_until_answered=True,
                
            )
        )

        logger.info("wait for the agent session start and participant join")
        await session_started
        participant = await ctx.wait_for_participant(identity=participant_identity)
        logger.info(f"participant joined: {participant.identity}")

        agent.set_participant(participant)

    except api.TwirpError as e:
        logger.error(
            f"error creating SIP participant: {e.message}, "
            f"SIP status: {e.metadata.get('sip_status_code')} "
            f"{e.metadata.get('sip_status')}"
        )
        ctx.shutdown()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
            initialize_process_timeout=60
        )
    )