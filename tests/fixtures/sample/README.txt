Committed fixture codebase of KNOWN, deterministic, network-free structure.
Shared by the extractor, indexer, store, CLI, and MCP tests. This is test
reference data (code to be parsed), legitimately living under tests/fixtures.

Python (5 modules):
  payments/__init__.py   -> module payments
  payments/charge.py     -> class Base (method run), class Charge(Base)
                            (method run); imports os
  payments/billing.py    -> from payments.charge import Charge; function bill
                            constructs Charge() and calls c.run(amount) (an
                            attribute-tail `run` call: it suffix-matches every
                            `run` method across the index, so those edges are
                            best_effort — fix 5); function charge
  payments/premium.py    -> from payments.charge import Charge;
                            class Premium(Charge) (method run)
  leaf.py                -> function unused (nobody imports or calls it, so
                            impact("leaf.unused").affected == [])

JS/TS (2 modules, exercising the relative-specifier resolver):
  web/charge.ts          -> export class Charge (method run)
  web/billing.ts         -> import { Charge } from "./charge"; function bill

No-indexable files (counted skipped, never errored):
  README.txt             -> this file
  assets/data.txt        -> a .txt in an otherwise empty sub-folder (fix 6)

The symbol "run" exists on payments.charge.Base, payments.charge.Charge, and
web/charge.Charge, and the symbol "Charge" exists in both payments.charge and
web/charge — the two overlaps that make the suffix/attribute-tail matches
best_effort and the bare symbol "Charge" ambiguous.
