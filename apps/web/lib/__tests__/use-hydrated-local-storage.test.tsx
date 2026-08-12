import { act, render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";

import {
  stringStorageCodec,
  useHydratedLocalStorage,
} from "@/lib/use-hydrated-local-storage";

function Harness() {
  const [value, setValue, hydrated] = useHydratedLocalStorage(
    "test:preference",
    "server-default",
    stringStorageCodec,
  );
  return (
    <button type="button" onClick={() => setValue("changed")}>
      {hydrated ? "hydrated" : "server"}:{value}
    </button>
  );
}

describe("useHydratedLocalStorage", () => {
  beforeEach(() => window.localStorage.clear());

  it("keeps browser-only data out of the server render", () => {
    window.localStorage.setItem("test:preference", "browser-value");

    const html = renderToString(<Harness />);
    expect(html).toContain("server-default");
    expect(html).not.toContain("browser-value");
  });

  it("restores and persists the browser value after mount", async () => {
    window.localStorage.setItem("test:preference", "browser-value");
    render(<Harness />);

    expect(await screen.findByText("hydrated:browser-value")).toBeInTheDocument();
    await act(async () => screen.getByRole("button").click());
    expect(screen.getByText("hydrated:changed")).toBeInTheDocument();
    expect(window.localStorage.getItem("test:preference")).toBe("changed");
  });
});
