"""Shared durable Outbox receipts; independent of Core and platform hosts."""

import json

from .onebot_client import bounded_post


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DeliveryReceipts:
    def start_delivery(self, item):
        from .account_actions import is_account
        from .native_actions import AccountAck

        if self.reconciliation_only:
            raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
        self.capacity(4096)
        fallback = {"lease_token": item["lease_token"], "result": "DELIVERY_UNCERTAIN",
                    "error_code": "BRIDGE_SEND_INTERRUPTED"}
        if is_account(item):
            fallback["account_action_receipt"] = AccountAck(
                item["action_type"], error_code="BRIDGE_SEND_INTERRUPTED").value(item)
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM results WHERE message_id=?", (item["message_id"],)
            ).fetchone():
                raise ValueError("DELIVERY_ALREADY_ATTEMPTED")
            self.db.execute(
                "INSERT INTO results(message_id,body) VALUES (?,?)",
                (
                    item["message_id"],
                    encode(fallback),
                ),
            )

    def finish_delivery(self, message_id, provider_id, *, error_code=None):
        from .native_actions import AccountAck, NativeAck

        with self.db:
            row = self.db.execute(
                "SELECT body FROM results WHERE message_id=?", (message_id,)
            ).fetchone()
            body = json.loads(row[0])
            if isinstance(provider_id, AccountAck):
                original = body.get("account_action_receipt")
                if not original or original["action_type"] != provider_id.action_type:
                    raise ValueError("INVALID_ACCOUNT_ACTION_RECEIPT")
                item = {"delivery_lane": "qq_account", "message_id": message_id,
                        "lease_token": body["lease_token"], "action_type": original["action_type"],
                        "content": {"payload_sha256": original["payload_sha256"]}}
                receipt = provider_id.value(item)
                body = {"lease_token": body["lease_token"], "result": "SENT" if provider_id
                        else "DELIVERY_UNCERTAIN", "provider_message_id": None,
                        "account_action_receipt": receipt}
                if receipt["error_code"]:
                    body["error_code"] = receipt["error_code"]
            elif isinstance(provider_id, NativeAck):
                body = {"lease_token": body["lease_token"], "result": "SENT",
                        "provider_message_id": None, "native_ack": provider_id.value()}
            elif isinstance(provider_id, str) and provider_id and len(provider_id) <= 256:
                body = {
                    "lease_token": body["lease_token"],
                    "result": "SENT",
                    "provider_message_id": provider_id,
                }
            if error_code == "IMAGE_PREFLIGHT_SKIPPED":
                body["error_code"] = error_code
            self.db.execute(
                "UPDATE results SET body=? WHERE message_id=?", (encode(body), message_id)
            )

    def cancel_delivery(self, item, receipt):
        """Record authenticated no-dispatch cancellation without manufacturing a send ACK."""
        from . import account_actions

        expected = {"cancelled": True, "message_id": item["message_id"], "state": "CANCELLED",
                    "send_attempt_retained": True}
        if account_actions.is_account(item):
            account_actions.cancelled_proof(receipt, message_id=item["message_id"],
                lease_token=item["lease_token"], content_sha256=item["content_sha256"],
                payload_sha256=item["content"]["payload_sha256"])
        elif receipt != expected:
            raise ValueError("NOT_SENT_CORE_PROOF_REQUIRED")
        prior = self.db.execute("SELECT body,state FROM results WHERE message_id=?",
                                (item["message_id"],)).fetchone()
        if prior:
            value = json.loads(prior[0])
            if (value.get("lease_token") != item["lease_token"]
                or value.get("result") not in {"DELIVERY_UNCERTAIN", "NOT_SENT"}
                or (prior[1] != "pending" and value.get("result") != "NOT_SENT")):
                raise ValueError("DELIVERY_ALREADY_ATTEMPTED")
        value = encode({"lease_token": item["lease_token"], "result": "NOT_SENT",
                        "core_receipt": receipt})
        with self.db:
            self.db.execute("INSERT INTO results(message_id,body,state) VALUES (?,?,'accepted') "
                            "ON CONFLICT(message_id) DO UPDATE SET body=excluded.body,"
                            "state='accepted'", (item["message_id"], value))

    async def flush_result(self, client, *, runtime=None):
        from .owner_resolution import pending_result

        row = pending_result(self, runtime=runtime)
        if not row:
            return False
        value = json.loads(row[1])
        from companion_delivery_protocol.preflight import KIND

        if value["result"] == KIND:
            from . import owner_preflight

            if runtime is None or runtime.spool is not self:
                raise ValueError("OWNER_RECEIPT_SPOOL_REQUIRED")
            await owner_preflight.flush(runtime, client, row[0], value)
            return True
        status, result = await bounded_post(
            client,
            f"/internal/v1/deliveries/{row[0]}/result",
            value,
            statuses=(200, 409),
            with_status=True,
        )
        if status == 409:
            # A human may already have reconciled it; never send it again.
            if result.get("error") not in {"STALE_DELIVERY_LEASE", "INVALID_DELIVERY_TRANSITION"}:
                raise ValueError("INVALID_CORE_RESULT_ACK")
            state = "rejected"
        else:
            expected = json.loads(row[1])["result"]
            if result != {"state": expected}:
                raise ValueError("INVALID_CORE_RESULT_ACK")
            state = "accepted"
        with self.db:
            self.db.execute("UPDATE results SET state=? WHERE message_id=?", (state, row[0]))
        # Receipt state is already committed with synchronous=FULL. Maintenance
        # must not wait for an observer's WAL snapshot on the async send path.
        # Input-text erasure retains its separate truncating checkpoint.
        self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return True
