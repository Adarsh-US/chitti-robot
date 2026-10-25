#!/usr/bin/env python3
"""Stage 4 push-to-talk voice input for Jarvis Home.

The microphone is controlled from the terminal: press Enter to start a
recording and press Enter again to stop it. Audio is transcribed locally with
faster-whisper and accepted transcripts are published to ``user/speech``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any


SAMPLE_RATE = 16_000
CHANNELS = 1
RMS_THRESHOLD = 0.01
DEFAULT_MODEL = "small"
SILENCE_HALLUCINATIONS = {
    "thank you",
    "you",
    "thanks",
    "thank you for watching",
}


class VoiceError(RuntimeError):
    """An expected voice-input or publishing failure."""


def _normalise_for_filter(text: str) -> str:
    """Case-fold text and remove punctuation and whitespace for filtering."""

    return "".join(character for character in text.casefold() if character.isalnum())


_NORMALISED_SILENCE_HALLUCINATIONS = {
    _normalise_for_filter(item) for item in SILENCE_HALLUCINATIONS
}


def _accepted_transcript(text: str) -> str | None:
    """Return a cleaned transcript, or ``None`` for empty/silence output."""

    cleaned = " ".join(text.split())
    if not cleaned or _normalise_for_filter(cleaned) in _NORMALISED_SILENCE_HALLUCINATIONS:
        return None
    return cleaned


def _load_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:
        raise VoiceError(
            "numpy is required for voice input; install dependencies with "
            "python -m pip install -r requirements.txt"
        ) from exc
    return np


def _load_sounddevice() -> Any:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise VoiceError(
            "sounddevice is required for microphone input; install dependencies with "
            "python -m pip install -r requirements.txt"
        ) from exc
    return sd


def _load_whisper_model(model_name: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceError(
            "faster-whisper is required for transcription; install dependencies with "
            "python -m pip install -r requirements.txt"
        ) from exc
    try:
        return WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as exc:
        raise VoiceError(f"could not load faster-whisper model {model_name!r}: {exc}") from exc


def _decode_wav_samples(wav_path: Path, np: Any) -> tuple[Any, int]:
    """Read PCM WAV samples for the RMS check and return samples plus rate."""

    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            raw = wav_file.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise VoiceError(f"could not read WAV file {wav_path}: {exc}") from exc

    if channels < 1 or sample_rate < 1:
        raise VoiceError(f"WAV file {wav_path} has invalid audio metadata")
    if sample_width == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        values = (values - 128.0) / 128.0
    elif sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8)
        if packed.size % 3:
            raise VoiceError(f"WAV file {wav_path} contains incomplete 24-bit samples")
        packed = packed.reshape(-1, 3)
        values = (
            packed[:, 0].astype(np.int32)
            | (packed[:, 1].astype(np.int32) << 8)
            | (packed[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values & 0x800000, values - 0x1000000, values).astype(np.float32)
        values /= 8388608.0
    elif sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise VoiceError(f"unsupported WAV sample width ({sample_width} bytes)")

    if values.size % channels:
        raise VoiceError(f"WAV file {wav_path} contains incomplete frames")
    if channels > 1 and values.size:
        values = values.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return values, sample_rate


def _rms(samples: Any, np: Any) -> float:
    if getattr(samples, "size", 0) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def _resample_to_model_rate(samples: Any, sample_rate: int, np: Any) -> Any:
    """Convert WAV samples to faster-whisper's 16 kHz model input rate."""

    if sample_rate == SAMPLE_RATE:
        return samples.astype(np.float32, copy=False)
    if getattr(samples, "size", 0) == 0:
        return samples.astype(np.float32, copy=False)
    target_size = max(1, round(samples.size * SAMPLE_RATE / sample_rate))
    source_positions = np.arange(target_size, dtype=np.float64) * sample_rate / SAMPLE_RATE
    left = np.floor(source_positions).astype(np.int64)
    right = np.minimum(left + 1, samples.size - 1)
    fraction = source_positions - left
    return (
        samples[left] * (1.0 - fraction) + samples[right] * fraction
    ).astype(np.float32)


def _record_microphone(sd: Any, np: Any, device: int | None) -> Any:
    """Record one terminal push-to-talk segment as 16 kHz mono float32 audio."""

    try:
        input("Press Enter to start recording.\n")
    except EOFError as exc:
        raise VoiceError("microphone mode needs an interactive terminal") from exc

    chunks: list[Any] = []
    callback_errors: list[str] = []

    def callback(indata: Any, frame_count: int, callback_time: Any, status: Any) -> None:
        del frame_count, callback_time
        if status:
            callback_errors.append(str(status))
        chunks.append(indata.copy())

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            device=device,
            callback=callback,
        ):
            print("Recording... press Enter to stop.", flush=True)
            try:
                input()
            except EOFError as exc:
                raise VoiceError("microphone mode needs an interactive terminal") from exc
    except VoiceError:
        raise
    except Exception as exc:
        raise VoiceError(f"microphone audio error: {exc}") from exc

    if callback_errors:
        print(f"microphone warning: {callback_errors[-1]}", file=sys.stderr)
    if not chunks:
        raise VoiceError("microphone returned no audio frames")
    try:
        return np.concatenate(chunks, axis=0).reshape(-1)
    except Exception as exc:
        raise VoiceError(f"could not assemble microphone audio: {exc}") from exc


def _transcribe(model: Any, audio: Any) -> str:
    try:
        segments, _info = model.transcribe(audio, language="en", vad_filter=True)
        return " ".join(segment.text for segment in segments).strip()
    except Exception as exc:
        raise VoiceError(f"transcription failed: {exc}") from exc


def _publish(host: str, port: int, text: str) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise VoiceError(
            "paho-mqtt is required to publish speech; install dependencies with "
            "python -m pip install -r requirements.txt"
        ) from exc

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"voice-stage4-{uuid.uuid4().hex[:12]}",
    )
    connected = False
    try:
        client.connect(host, port, keepalive=30)
        connected = True
        client.loop_start()
        payload = json.dumps(
            {"ts": float(time.time()), "text": text},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        info = client.publish("user/speech", payload, qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise VoiceError(f"MQTT publish failed: {mqtt.error_string(info.rc)}")
        wait_for_publish = getattr(info, "wait_for_publish", None)
        if callable(wait_for_publish):
            wait_for_publish(timeout=5.0)
        is_published = getattr(info, "is_published", None)
        if callable(is_published) and not is_published():
            raise VoiceError("MQTT publish timed out")
    except VoiceError:
        raise
    except Exception as exc:
        raise VoiceError(f"MQTT connection or publish failed at {host}:{port}: {exc}") from exc
    finally:
        if connected:
            try:
                client.disconnect()
            finally:
                client.loop_stop()


def _list_devices() -> int:
    sd = _load_sounddevice()
    try:
        print(sd.query_devices())
    except Exception as exc:
        raise VoiceError(f"could not list audio devices: {exc}") from exc
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true", help="list sounddevice audio devices and exit")
    parser.add_argument("--device", type=int, default=None, help="microphone input device index")
    parser.add_argument("--file", type=Path, default=None, metavar="WAV", help="transcribe a WAV file instead of recording")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="faster-whisper model name (default: small)")
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_devices and (args.file is not None or args.device is not None):
        print("voice input error: --list-devices cannot be combined with --file or --device", file=sys.stderr)
        return 2
    if args.device is not None and args.device < 0:
        print("voice input error: --device must be a non-negative index", file=sys.stderr)
        return 2
    if args.port <= 0 or args.port > 65535:
        print("voice input error: --port must be 1-65535", file=sys.stderr)
        return 2
    if not args.model.strip():
        print("voice input error: --model must not be empty", file=sys.stderr)
        return 2

    try:
        if args.list_devices:
            return _list_devices()

        if args.file is not None:
            if not args.file.is_file():
                raise VoiceError(f"WAV file does not exist: {args.file}")
        np = _load_numpy()
        if args.file is not None:
            samples, sample_rate = _decode_wav_samples(args.file, np)
            audio: Any = _resample_to_model_rate(samples, sample_rate, np)
        else:
            sd = _load_sounddevice()
            audio = _record_microphone(sd, np, args.device)
            samples = audio

        level = _rms(samples, np)
        if level < RMS_THRESHOLD:
            print(f"Audio below RMS threshold ({level:.5f} < {RMS_THRESHOLD:.5f}); skipping transcription.")
            return 0

        model = _load_whisper_model(args.model)
        transcript = _accepted_transcript(_transcribe(model, audio))
        if transcript is None:
            return 0
        print(f"Accepted transcript: {transcript}", flush=True)
        _publish(args.host, args.port, transcript)
    except VoiceError as exc:
        print(f"voice input error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("voice input stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
