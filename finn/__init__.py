"""FINN prototype core - ENAM build challenge.

Deterministic, testable core of the research plan: typed extraction schemas,
announcement dedupe/revision handling, accounting comparability checks, the
guidance ledger, and threshold alerts. LLM work happens only inside the
Pydantic AI agents in agents.py; every comparison is plain Python.
"""
