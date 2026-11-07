from companion import evaluation
from scripts import evaluate_image_understanding as experiment
from tests.support.evaluation import prepare


def prepared(core, tmp_path):
    policy = experiment.planning_settings().paid_model.image_input
    prepare(core, tmp_path, image_input=policy, daily_calls=40, window_calls=40)
    suite = experiment.prepare(tmp_path / "prepared", settings=core.settings,
                               subset=experiment.load_cases()[:1])
    paths = sorted((tmp_path / "prepared").glob("manifest-*.json"))
    manifests = [evaluation.Manifest.model_validate_json(path.read_text("utf-8")) for path in paths]
    return suite, manifests
