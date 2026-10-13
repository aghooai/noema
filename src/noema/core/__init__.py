"""Frozen core of Noema: value types, errors, text helpers and the Protocols every module codes against (spec-02).

`noema.core` imports nothing from `noema` except itself (spec-01 §2 rule 1). Value types are frozen, slotted
dataclasses; Protocols are plain `typing.Protocol` classes (not runtime-checkable).
"""
