#!/usr/bin/env python3
"""Receive and decode H.264 video through the Home Assistant WebRTC adapter."""

import argparse
import asyncio
import json
import statistics

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.jitterbuffer import JitterBuffer


async def check(args, stream):
    """Require a real decoded, non-grey frame, not just successful signaling."""
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    result = asyncio.get_running_loop().create_future()

    @pc.on("track")
    async def on_track(track):
        if track.kind != "video":
            return
        try:
            frame = await track.recv()
            value = {
                "stream": stream,
                "width": frame.width,
                "height": frame.height,
                "pixel_std": round(
                    statistics.pstdev(bytes(frame.to_rgb().planes[0])[::1024]), 2
                ),
            }
            if not result.done():
                result.set_result(value)
        except Exception as error:
            if not result.done():
                result.set_exception(error)

    try:
        transceiver = pc.addTransceiver("video", direction="recvonly")
        # aiortc 1.14.0's default 128-packet buffer cannot fit large HD I-frames.
        # Pin that version in Dockerfile.relay-test because this is a private API.
        transceiver.receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(
            capacity=4096, is_video=True
        )
        transceiver.setCodecPreferences(
            [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType.lower() == "video/h264"
            ]
        )
        await pc.setLocalDescription(await pc.createOffer())
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                args.url,
                params={"src": stream},
                data=pc.localDescription.sdp,
                headers={"Origin": args.origin, "Content-Type": "application/sdp"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response,
        ):
            answer = await response.text()
            if response.status not in (200, 201):
                raise RuntimeError(f"{stream}: SDP HTTP {response.status}")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        value = await asyncio.wait_for(result, timeout=30)
        if value["pixel_std"] <= 5:
            raise RuntimeError(f"{stream}: received a grey/blank frame: {value}")
        print(json.dumps(value), flush=True)
    finally:
        await pc.close()


async def main():
    """Test streams sequentially to avoid simultaneous HD transcode load."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Full adapter SDP endpoint")
    parser.add_argument("--origin", required=True, help="Allowed Home Assistant origin")
    parser.add_argument("--streams", nargs="+", required=True)
    args = parser.parse_args()
    for stream in args.streams:
        await check(args, stream)


if __name__ == "__main__":
    asyncio.run(main())
