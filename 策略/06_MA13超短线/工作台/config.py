# -*- coding: utf-8 -*-
"""
config.py —— 读取 workbench.json，构建策略 + 闸门上下文。

不依赖 tqsdk；纯配置解析，可离线用。
"""
from __future__ import annotations
import json
import os
from strategies import build_strategy, build_signal
from session_gate import SessionGate
from sizer import get_sizer


def default_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbench.json")


def load_workbench(path: str | None = None) -> dict:
    path = path or default_config_path()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_context(cfg: dict) -> dict:
    """
    返回 {"strategy": Strategy, "gate": SessionGate,
          "signal": Signal, "sizer": Sizer, "cfg": cfg}，
    供 run_workbench / ma13_live_multi 使用。
    signal（Stage 1）与 sizer（Stage 3）均可插拔。
    """
    strategy = build_strategy(cfg)
    gate_cfg = cfg.get("session_gate", {}) or {}
    if gate_cfg.get("enabled", True):
        gate = SessionGate(
            k=float(gate_cfg.get("k", 1.5)),
            n_sessions=int(gate_cfg.get("n_sessions", 20)),
        )
    else:
        gate = None
    strategy.set_gate(gate)
    signal = build_signal(cfg)
    sizer = get_sizer(cfg.get("sizer"))
    return {"strategy": strategy, "gate": gate,
            "signal": signal, "sizer": sizer, "cfg": cfg}
