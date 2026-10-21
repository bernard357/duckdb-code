import { Charge } from "./charge";

export function bill(amount: number): number {
    const c = new Charge();
    return c.run(amount);
}
