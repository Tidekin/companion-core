from companion import qzone_publication as publication
from companion.social_autonomy import SocialAutonomy
from tests.support.life_expression_consumption import prepared


def setup(core, tmp_path, *, private=False):
    job, _, ref, source = prepared(core, tmp_path, private=private,
        purposes=["social", "account_actions", "qzone_publication"],
        social_options={"qzone_public_enabled": True})
    data = core.life_topics.retained_result(job)
    module = SocialAutonomy(core.store, core.settings, clock=core.clock)
    body = publication.PublicationCreate(job_id=job,
        expected_content_sha256=data["qzone_draft"]["content_sha256"])
    return module, body, ref, source
