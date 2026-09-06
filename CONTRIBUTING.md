# Contributing to Tado CE

Thanks for your interest in contributing.

## Development Setup

```bash
git clone https://github.com/hiall-fyi/tado_ce.git
cd tado_ce
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

To test locally in Home Assistant, symlink or copy `custom_components/tado_ce/` into your HA config's `custom_components/` directory and restart.

## Testing

```bash
pytest              # Full test suite
ruff check .        # Lint
mypy custom_components/tado_ce/   # Type check
```

`ruff.toml` and `pyproject.toml`'s mypy settings aren't part of this repository (see the comment at the top of `requirements-dev.txt` for why), so `ruff check`/`mypy` without a local config check against tool defaults rather than the exact ruleset a maintainer runs locally. Don't worry about matching that ruleset exactly; a PR gets reviewed against it either way.

## Project Structure

Everything lives under `custom_components/tado_ce/`, roughly grouped as:

- **Platform entities** — `climate_heating.py`, `climate_ac.py`, `sensor_*.py`, `binary_sensor.py`, `switch.py`, `select.py`, `number.py`, `button.py`, `calendar.py`, `water_heater.py`, `device_tracker.py`
- **Setup & coordination** — `__init__.py`, `coordinator.py`, `entry_lifecycle.py`, `setup_entry_helpers.py`, `config_flow.py`, `config_flow_options.py`, `config_manager.py`, `entity_registry.py`, `entity_cleanup.py`
- **Tado API access** — `api_client.py`, `api_auth.py`, `bridge_api.py`, `bridge_discovery.py`, `data_loader.py`, `polling.py`, `ratelimit.py`
- **Controllers** — `offset_sync_controller.py`, `valve_controller.py`, `heating_coordinator.py`, `weather_compensation.py`, `write_optimizer.py`
- **HomeKit local control** — `homekit_client.py`, `homekit_provider.py`, `homekit_mapping.py`
- **Insights & analytics** — `insights_*.py`, `heating_analyzer.py`, `heating_detector.py`, `thermal_analyzer.py`, `smart_comfort.py`, `calculations.py`
- **Services** — `services.py`, `services_helpers.py`, `services.yaml`

## Pull Request Guidelines

- Keep PRs focused on a single change
- Add tests for new functionality (aim for the same coverage the file already has)
- Run `pytest`, `ruff check`, and `mypy` before submitting
- Update `CHANGELOG.md` with your change, under the current `[Unreleased]`/in-progress section
- No ticket/issue numbers in source comments, docstrings, or log strings — CHANGELOG and the PR description are where those belong

## Code Style

- Python 3.13+, `from __future__ import annotations`
- Lazy logging (`_LOGGER.debug("...%s", value)`, never an f-string) so a disabled log level costs nothing to format
- Event-listener callbacks registered with HA (`async_track_state_change_event` etc.) must be decorated `@callback` — a missing decorator only fails at runtime on a real event loop, not under pytest's mocked `hass`
- A broad `except Exception` needs an inline comment explaining why it's broad, and must log at least once — a silently swallowed error is never acceptable

## Reporting Issues

Please use the [issue templates](https://github.com/hiall-fyi/tado_ce/issues/new/choose) and include:
- Your Tado CE and Home Assistant version
- Whether HomeKit local control is enabled
- A debug log if the report involves unexpected behaviour, not just a screenshot
- Steps to reproduce
