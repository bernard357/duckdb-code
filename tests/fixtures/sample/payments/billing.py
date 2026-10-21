"""Billing imports Charge and calls a local helper."""

from payments.charge import Charge


def bill(amount):
    c = Charge()
    return charge(c.run(amount))


def charge(amount):
    return amount
