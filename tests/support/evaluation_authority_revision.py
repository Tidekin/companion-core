from companion import owner_authority
from companion.owner_authority_protocol import AuthorityStop
from tests.support.owner_authority import activate


def stop(core, case, operation="evaluation-revision-stop"):
    return owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id=operation, authority=case.successor["authority"]))


def revised_policy(case):
    return case.policy.model_copy(update={
        "purposes": [*case.policy.purposes, "evaluation"], "authorization_sha256": "a" * 64})


def revise(core, case):
    stop(core, case)
    policy = revised_policy(case)
    result, request = activate(core, policy, revision=case.successor["authority"]["revision"] + 1,
                               operation="evaluation-revision-activate")
    case.successor, case.policy = result, policy
    core.authorized_model_test_capture = result, case.key
    core.authorized_model_test_jobs = {}
    return request
