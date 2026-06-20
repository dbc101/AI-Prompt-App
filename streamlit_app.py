from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT_APP_PATH = Path(__file__).resolve().parent.parent / "streamlit_app.py"


def _load_root_app():
    spec = importlib.util.spec_from_file_location("agent_prompt_builder_root", ROOT_APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load root Streamlit app from {ROOT_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_root_app().main


if __name__ == "__main__":
    main()
