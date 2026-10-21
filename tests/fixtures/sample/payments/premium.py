"""Premium subclasses Charge."""

from payments.charge import Charge


class Premium(Charge):
    def run(self, amount):
        return amount * 2
