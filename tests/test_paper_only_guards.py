from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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
import ubuntu_paper_supervisor as ups  # noqa: E402


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


def test_find_paper_tbank_token_ignores_generic_env_and_desktop_sources(monkeypatch, tmp_path):
    attempts: list[str] = []

    class DummyUsers:
        def get_accounts(self):
            return []

    class DummyClient:
        def __init__(self, token: str):
            self.token = token
            self.users = DummyUsers()

        def __enter__(self):
            attempts.append(self.token)
            if self.token != "readonly-token":
                raise RuntimeError("unexpected_token")
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_invest = types.ModuleType("t_tech.invest")
    fake_invest.Client = DummyClient
    fake_root = types.ModuleType("t_tech")
    fake_root.invest = fake_invest
    monkeypatch.setitem(sys.modules, "t_tech", fake_root)
    monkeypatch.setitem(sys.modules, "t_tech.invest", fake_invest)
    monkeypatch.setenv("TBANK_TOKEN_READONLY", "readonly-token")
    monkeypatch.setenv("TBANK_TOKEN", "generic-token")
    monkeypatch.setenv("TINKOFF_TOKEN", "legacy-token")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "token.txt").write_text("t.desktop-token-12345678901234567890", encoding="utf-8")
    monkeypatch.setattr(ngsb.Path, "home", lambda: tmp_path)

    token = ngsb.find_paper_tbank_token()

    assert token == "readonly-token"
    assert attempts == ["readonly-token"]


def test_paper_launchers_still_pass_explicit_paper_only_flag():
    for path in [
        ROOT / "scripts" / "ubuntu_paper_supervisor.py",
        ROOT / "scripts" / "run_v7_paper_contours_20260525.ps1",
        ROOT / "scripts" / "watch_v7_paper_contours_20260525.ps1",
    ]:
        assert "--paper-only" in path.read_text(encoding="utf-8")


def test_supervisor_bot_args_force_paper_only_for_every_portfolio(tmp_path):
    args = types.SimpleNamespace(
        project_root=str(tmp_path),
        python="python",
        dashboard_host="127.0.0.1",
        dashboard_port="8768",
        loop_sec=15,
        stale_sec=90,
        startup_grace_sec=180,
        once=True,
    )
    supervisor = ups.Supervisor(args)
    for name, secids in ups.PORTFOLIOS.items():
        argv = supervisor.bot_args(name, secids[:1] or ["TEST"])
        assert argv[0] == "src/multi_futures_paper.py"
        assert "--paper-only" in argv
        assert "--entry-shadow-log" in argv
        assert not any(str(part).lower().startswith("--live") for part in argv)


def test_paper_runtime_paths_do_not_reference_live_order_apis():
    forbidden_tokens = [
        "client.orders",
        "client.stop_orders",
        "post_order(",
        "post_stop_order(",
        "cancel_order(",
        "replace_order(",
    ]
    guarded_paths = [
        ROOT / "src" / "ng_scalper_bot.py",
        ROOT / "src" / "multi_futures_paper.py",
        ROOT / "src" / "leadlag_ng_paper_orderbook_monitor.py",
        ROOT / "src" / "reconcile_paper_positions.py",
        ROOT / "scripts" / "ubuntu_paper_supervisor.py",
        ROOT / "scripts" / "run_v7_paper_contours_20260525.ps1",
        ROOT / "scripts" / "watch_v7_paper_contours_20260525.ps1",
        ROOT / "scripts" / "run_ng_paper_once_20260524.ps1",
        ROOT / "scripts" / "run_scalp_paper_once_20260524.ps1",
    ]

    for path in guarded_paths:
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"{path} unexpectedly references live order API token: {token}"
