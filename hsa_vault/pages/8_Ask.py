"""Ask questions about your own vault, in plain English.

The model is never asked to do arithmetic — every figure it may quote is computed
by `ledger` and handed to it as fact. See core/assistant.py for why.
"""

import streamlit as st

from core import assistant, auth, store

st.set_page_config(page_title="Ask — HSAVault", page_icon="💬", layout="wide")

auth.require_login()
st.title("💬 Ask your vault")
store.show_flash()

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

settings = store.settings()
receipts = store.receipts()
store.show_offline()

_CHAT = "_hsa_chat"

if not settings.nvidia_api_key:
    st.warning(
        "No NVIDIA API key configured, so there is nothing to answer with. "
        "Add one on the **Settings** page — everything else in the app works without it."
    )
    st.stop()

if not receipts:
    st.info("Nothing to ask about yet. Add a receipt on the **Upload** page.")
    st.stop()

st.caption(
    f"Answers are grounded in your {len(receipts)} receipt(s). Balances are computed by the "
    "app and handed to the model as fact — it is not asked to add anything up. "
    "Your receipt details are sent to NVIDIA to answer, the same as receipt images are "
    "during extraction. **Not tax advice.**"
)

history = st.session_state.setdefault(_CHAT, [])

# --- suggestions ------------------------------------------------------------
# A blank chat box is a hard place to start, and these double as a demonstration
# of what the vault can actually answer.
SUGGESTIONS = [
    "What can I still reimburse myself for?",
    "What did I spend the most on this year?",
    "Which receipts need my attention?",
    "Summarise my vault in three sentences.",
]

if not history:
    st.write("**Try one of these:**")
    columns = st.columns(len(SUGGESTIONS))
    for column, text in zip(columns, SUGGESTIONS):
        if column.button(text, width="stretch", key=f"sug_{text[:14]}"):
            st.session_state["_hsa_pending_question"] = text
            st.rerun()

for message in history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

question = st.chat_input("Ask about your receipts…") or st.session_state.pop(
    "_hsa_pending_question", None
)

if question:
    history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Reading your vault…"):
            try:
                reimbursements = store.reimbursements()
            except Exception:  # noqa: BLE001 - the withdrawals tab is optional context
                reimbursements = []
            reply, error = assistant.answer(
                question,
                assistant.build_context(receipts, reimbursements),
                settings.nvidia_api_key,
                settings.nvidia_chat_model,
                settings.nvidia_base_url,
                history[:-1],
            )
        if error:
            st.error(f"Could not answer: {error}")
            # Popped rather than left behind: a question with no answer under it
            # reads like the app lost the reply.
            history.pop()
        else:
            st.markdown(reply)
            history.append({"role": "assistant", "content": reply})

if history and st.button("Clear conversation"):
    st.session_state[_CHAT] = []
    st.rerun()

st.divider()
st.caption("Not tax advice. Answers come from your own receipts, not from the internet.")
