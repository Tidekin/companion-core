# Local daily voice replies

The voice node uses a resident, loopback-only speech service: GPT-SoVITS reads the
reply, then Applio converts its timbre. Replies do not use Edge TTS or a cloud speech
provider. Core still owns authorization, job identity, deadlines and delivery;
the service only accepts bounded text and returns PCM16 WAV in memory.

The service implements `POST /v1/audio/speech` with a fixed `model`, `voice`,
`response_format=wav` and `speed=1`. Base reading speed belongs to the pinned
GPT-SoVITS `speed_factor` configuration. Unsupported fields and streaming are
rejected. `GET /healthz` becomes ready after both models load and a synthetic
warm-up finishes. Its manifest identifies the actual model, index, reference,
inference source and local service bytes. It is an operator consistency check,
not remote attestation.

## Configuration and operation

Keep private configuration, weights, caches and sample outputs inside the project
runtime directory. A `SpeechServiceConfig` JSON contains the base `GPTSoVITSConfig`,
absolute `gpt_weights`, `sovits_weights`, `applio_source`, `model_path`, `index_path`,
the pinned `gpt_revision` and `applio_revision`, and the selected model/voice IDs.
The native GPT server and the resident service use separate loopback ports (for
example 19881 and 19880). The native launcher suppresses upstream transcript and
phoneme console output; it does not log private reply text.

The node profile's `tts` is now `OpenAISpeechConfig`: loopback `base_url`, fixed
`model`/`voice`, single concurrency and bounded WAV output. Replace the former
outer `tts_gpt_weights`/`tts_sovits_weights` with `tts_service_config` pointing to
the private service JSON, and set `tts_server_revision` to the frozen node source
revision. `configure-voice-node.py` writes the manifest without contacting Core.
Start with these scripts from the same frozen checkout:

```powershell
./scripts/start-voice-tts.ps1 -RuntimeRoot $runtime -Port 19881
./scripts/start-local-speech.ps1 -RuntimeRoot $runtime -Config $speechConfig -Port 19880
./scripts/configure-voice-node.py --runtime-root $runtime --workspace $checkout --profile $profile --connection $connection
./scripts/start-voice-node.ps1 -RuntimeRoot $runtime -NodeConfig $nodeConfig
```

The GPT launcher reads `$runtime/tts-infer.yaml`. Invoke Python scripts using the
prepared runtime Python. Verify the node manifest
in Core and its current OwnerAuthority policy before starting claims. A changed
manifest requires the existing reviewed configuration and authority rollout;
editing an ACTIVE policy, reusing its old fingerprint, or resetting the journal
does not register a replacement node. Preserve the original quiescent node journal
and use its native schema migration. Existing UNKNOWN and cleanup custody remain
binding across source upgrades.

An optional literal unicast `core_connect_address` pins the Core TCP destination.
The original HTTPS hostname remains the HTTP Host and TLS SNI, with normal
certificate verification. This reuses the existing Bridge networking pattern and
does not change public DNS or accept a different authority. The reverse proxy must
forward `/internal/v2/voice-node/`, retaining authorization and idempotency headers.

The service retains its single slot until native inference finishes, including
after HTTP timeout or cancellation. An uncertain base execution quarantines the
service. Stop new node work, resolve original Core/node custody, and establish
quiescence before restarting via `stop-voice-runtime.ps1 -Service speech` or `tts`.
Restarting a provider never proves a cancelled or UNKNOWN job completed.

## Recorded provider versions and checks

The accepted local trial uses [Applio 3.7.1](https://github.com/IAHispano/Applio/tree/6f69726b140c615c8cc6831a7db8b7c5e48ba8af)
and [GPT-SoVITS v2Pro](https://github.com/RVC-Boss/GPT-SoVITS/tree/d7c2210da8c013e81a94bfc7b811a477c99fd506).
The service calls the pinned native VoiceConverter pipeline with local ContentVec
and RMVPE assets; it does not implement another voice conversion algorithm.
The accepted 32 kHz model uses pitch 0, index rate 0.35, protect 0.33, volume
envelope 1 and seed 42. Keep the accepted model/index hashes in the runtime receipt.

ASR uses local faster-whisper. Its
[decoder timestamps](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py)
and [VAD timestamp restoration](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/vad.py)
can estimate an end beyond the captured PCM. A real 5.56-second sample produced
an end of 5.62 seconds. The adapter clamps at most 100 ms of estimated tail to the
actual captured duration; nonfinite, reversed, overlapping, wholly outside or
larger-overrun evidence remains invalid. This never extends source duration.

Relevant checks cover bounded WAV parsing, request restrictions, timeout slot
ownership, quarantine, pinned TLS authority, a real independent supervised child
using the native HTTP service, and the existing signed Core/independent worker/
mock-OneBot chain. Real provider acceptance uses explicit synthetic sample text
with `verify-voice-runtime.py`; it records confirmed child exit and never sends QQ.
