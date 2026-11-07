from tests.support.live_image_input import options as image_options
from tests.support.private_companion import companion_options


def scope(core):
    options, images = companion_options(core), image_options()
    options.update(image_scope=images["image_scope"],
                   image_approval_sha256=images["image_approval_sha256"])
    options["core_options"].update(images["core_options"])
    options["paid_options"]["image_input"] = images["paid_options"]["image_input"]
    return options
