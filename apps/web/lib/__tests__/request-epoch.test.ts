import { describe, expect, it } from "vitest";

import { createRequestEpoch } from "@/lib/request-epoch";

describe("request epoch", () => {
  it("accepts only the latest overlapping request", () => {
    const requests = createRequestEpoch();
    const first = requests.begin();
    const second = requests.begin();

    expect(requests.isCurrent(first)).toBe(false);
    expect(requests.isCurrent(second)).toBe(true);
  });

  it("invalidates an outstanding request when its UI scope closes", () => {
    const requests = createRequestEpoch();
    const token = requests.begin();

    requests.invalidate();

    expect(requests.isCurrent(token)).toBe(false);
  });

  it("prevents an older response from winning when promises resolve out of order", async () => {
    const requests = createRequestEpoch();
    const writes: string[] = [];
    let resolveFirst!: (value: string) => void;
    let resolveSecond!: (value: string) => void;
    const firstResponse = new Promise<string>((resolve) => { resolveFirst = resolve; });
    const secondResponse = new Promise<string>((resolve) => { resolveSecond = resolve; });

    const run = async (response: Promise<string>) => {
      const token = requests.begin();
      const value = await response;
      if (requests.isCurrent(token)) writes.push(value);
    };
    const firstRun = run(firstResponse);
    const secondRun = run(secondResponse);

    resolveSecond("resource-b");
    await secondRun;
    resolveFirst("resource-a");
    await firstRun;

    expect(writes).toEqual(["resource-b"]);
  });
});
