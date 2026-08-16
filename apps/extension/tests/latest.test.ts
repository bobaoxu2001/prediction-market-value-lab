import { describe, expect, it } from "vitest";

import { Latest } from "../src/latest";

describe("Latest (last-request-wins guard)", () => {
  it("the first token is current until another unit begins", () => {
    const latest = new Latest();
    const first = latest.begin();
    expect(latest.isCurrent(first)).toBe(true);
    const second = latest.begin();
    expect(latest.isCurrent(first)).toBe(false);
    expect(latest.isCurrent(second)).toBe(true);
  });

  it("an interrupted chain still supersedes: only the newest token writes", async () => {
    const latest = new Latest();
    const writes: string[] = [];

    // The shape of reload(): unit A begins, awaits, then checks before writing.
    const unitA = async () => {
      const token = latest.begin();
      await Promise.resolve();
      if (latest.isCurrent(token)) writes.push("a");
    };
    const unitB = async () => {
      const token = latest.begin();
      await Promise.resolve();
      if (latest.isCurrent(token)) writes.push("b");
    };

    const promiseA = unitA();
    await unitB(); // B begins and completes while A is still awaiting
    await promiseA; // A completes last but must not write

    expect(writes).toEqual(["b"]);
  });

  it("a stale token never becomes current again", () => {
    const latest = new Latest();
    const token = latest.begin();
    latest.begin();
    latest.begin();
    expect(latest.isCurrent(token)).toBe(false);
  });
});
