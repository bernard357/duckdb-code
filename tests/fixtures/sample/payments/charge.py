"""A base charge and its concrete Charge class."""

import os


class Base:
    """The base charge."""

    def run(self, amount):
        return amount


class Charge(Base):
    """A concrete charge."""

    def run(self, amount):
        pid = os.getpid()
        return amount + pid
