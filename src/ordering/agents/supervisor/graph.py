"""Supervisor LangGraph wiring.

    START → guard ─┬─ blocked ─────────────────────────────────────────────┐
                   └→ prepare → classify ─┬→ recommendation ──────────────→ finalize ─┬→ END
                                          ├→ propose ─────────────────────→ finalize ─┴→ approval_gate (interrupt)
                                          ├→ decide_in_chat ─┬→ execute ─→ finalize        │
                                          │                  └→ rejected ─→ finalize        │
                                          ├→ order_status / respond ─────→ finalize        │
                                          └→ prepare_retry / prepare_cancel ─┬→ execute     │
                                                                             └→ finalize    │
    approval_gate ── resume(approve) → execute │ resume(reject) → rejected │ resume(deferred msg) → guard

State is checkpointed in Postgres after every node (``AsyncPostgresSaver``), so a crash or
restart resumes from the last completed step; approval pauses survive restarts indefinitely.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ordering.agents.supervisor import nodes
from ordering.agents.supervisor.state import SupervisorState


def build_graph(checkpointer=None):
    g = StateGraph(SupervisorState)
    for name in ("guard", "prepare", "classify", "recommendation", "propose", "approval_gate", "decide_in_chat",
                 "execute", "rejected", "order_status", "prepare_retry", "prepare_cancel", "respond", "finalize"):
        g.add_node(name, getattr(nodes, name))

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", nodes.route_after_guard, ["prepare", "finalize"])
    g.add_edge("prepare", "classify")
    g.add_conditional_edges("classify", nodes.route_after_classify,
                            ["recommendation", "propose", "decide_in_chat", "order_status", "prepare_retry",
                             "prepare_cancel", "respond"])
    for name in ("recommendation", "propose", "rejected", "order_status", "respond", "execute"):
        g.add_edge(name, "finalize")
    g.add_conditional_edges("decide_in_chat", nodes.route_after_decide, ["execute", "rejected", "finalize"])
    g.add_conditional_edges("prepare_retry", nodes.route_after_prepare, ["execute", "finalize"])
    g.add_conditional_edges("prepare_cancel", nodes.route_after_prepare, ["execute", "finalize"])
    g.add_conditional_edges("approval_gate", nodes.route_after_gate, ["execute", "rejected", "guard"])
    g.add_conditional_edges("finalize", nodes.route_after_finalize, ["approval_gate", END])
    return g.compile(checkpointer=checkpointer)
