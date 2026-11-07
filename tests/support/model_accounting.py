import hashlib

from companion import model_accounting


def proposal(tmp_path, request_id, *, operation="estimate-first", basis=None):
    private = tmp_path / (operation + ".evidence")
    private.write_bytes(("private-synthetic-billing-evidence:" + operation).encode())
    return {
        "version": 1, "operation_id": operation, "request_id": request_id,
        "authorization_sha256": "a" * 64,
        "evidence_sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
        "basis": basis or {"kind": "owner_estimate", "reference_provider": "reference",
            "reference_model": "reference-text", "currency": "USD",
            "input_rate": 2_000_000, "output_rate": 4_000_000},
    }, private


def prepared(core, tmp_path, request_id, **options):
    value, private = proposal(tmp_path, request_id, **options)
    return model_accounting.preview(core.store, core.settings, value, private, core.clock)[
        "prepared_request"], private
