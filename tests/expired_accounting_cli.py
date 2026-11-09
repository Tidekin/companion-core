"""Real offline CLI; only external Docker observation uses synthetic identities."""

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion import model_carry_controller  # noqa: E402
from companion.__main__ import main  # noqa: E402
from companion.config import Settings  # noqa: E402
from tests.model_carry_support import observer  # noqa: E402

settings = pickle.loads(Path(sys.argv[1]).read_bytes())
Settings.load = classmethod(lambda cls, **kwargs: settings)
native_verify = model_carry_controller.verify
model_carry_controller.verify = lambda items: native_verify(items, observe=observer(items))
sys.argv = [sys.argv[0], *sys.argv[2:]]
main()
