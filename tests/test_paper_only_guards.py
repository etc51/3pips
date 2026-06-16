from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if "requests" not in sys.modules:
    sys.modules["requests"] = types.ModuleType("requests")
if "statsmodels" not in sys.modules:
    sys.modules["statsmodels"] = types.ModuleType("statsmodels")
if "statsmodels.api" not in sys.modules:
    sys.modules["statsmodels.api"] = types.ModuleType("statsmodels.api")
if "matplotlib" not in sys.modules:
    sys.modules["matplotlib"] = types.ModuleType("matplotlib")
if "matplotlib.pyplot" not in sys.modules:
    sys.modules["matplotlib.pyplot"] = types.ModuleType("matplotlib.pyplot")

import leadlag_ng_paper_orderbook_monitor as leadlag  # noqa: E402
import multi_futures_paper as mfp  # noqa: E402
import ng_scalper_bot as ngsb  # noqa: E402


@pytest.mark.parametrize(
    ("parser", "argv"),
    [
        (ngsb.parse_args, []),
        (mfp.parse_args, []),
        (leadlag.parse_args, []),
    ],
)
def test_paper_entrypoints_require_explicit_paper_only(parser, argv):
    with pytest.raises(SystemExit):
        parser(argv)


def test_ng_scalper_accepts_paper_only_flag():
    args = ngsb.parse_args(["--paper-only"])
    assert args.paper_only is True


def test_multi_futures_accepts_paper_only_flag():
    args = mfp.parse_args(["--paper-only"])
    assert args.paper_only is True


def test_leadlag_accepts_paper_only_flag():
    cfg = leadlag.parse_args(["--paper-only"])
    assert cfg.paper_only is True
