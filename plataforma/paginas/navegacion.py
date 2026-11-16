"""Registro de páginas (lo llena app.py) para poder navegar entre ellas con st.switch_page."""
PAGINAS = {}


def ir_a_tenant(tenant_id):
    import streamlit as st
    st.session_state["tenant_sel"] = tenant_id
    st.switch_page(PAGINAS["tenant"])
