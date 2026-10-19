"""Product entry point; the previous workspace remains available for old manual runs."""
from pathlib import Path
import runpy
import streamlit as st

if st.query_params.get("view") == "legacy":
    runpy.run_path(str(Path(__file__).with_name("ui_legacy.py")), run_name="__main__")
else:
    from dzen_agent.ui_app import main
    main()
