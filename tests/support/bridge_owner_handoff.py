import httpx

from companion_onebot_bridge import journal


def core_handler(config, successor):
    def handler(request):
        if request.url.path.endswith("stop-receipt"):
            return httpx.Response(200, json={"authority": config.owner_authority,
                "state": "STOPPED",
                "stopped_authority": config.owner_authority | {"revision": 2},
                "core_stop_sha256": "e" * 64, "budget_journal_id": "budget_" + "f" * 32,
                "quiescence_proven": False})
        return httpx.Response(200, json={"approved": True, "authority": successor,
            "purpose": "chat", "expires_at": None, "budget_journal_id": "budget_" + "f" * 32,
            "capability_policies": {},
            "capture": {"public_key": config.capture_public_key,
                "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
                "protocol_epoch": config.protocol_epoch}})
    return handler
